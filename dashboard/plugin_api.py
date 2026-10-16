"""Hermes.Crew dashboard plugin - the board and card pages, served in-process.

Mounted at /api/plugins/crew/ by the dashboard plugin system
(hermes_cli.web_server._mount_plugin_api_routes), behind Hermes's own auth
middleware: every route here needs the dashboard session token (header) or the
OAuth session cookie, exactly like any other /api/ route. The tab
(dist/index.js) fetches pages with the SDK's authedFetch and renders them into
an iframe via srcdoc; the bridge script injected below routes the page's own
fetch/XHR, card navigation and avatar loads back through that same authedFetch,
so nothing inside the iframe ever needs a credential of its own.

Every page, JSON document, face and ack is built here, in the Hermes web server
process, by crew's own builders in <plugin>/scripts/crew_graph_serve.py (the same
functions the standalone daemon calls): no daemon has to run, so this works where
there is no systemd (Windows) and is the base a remote backend builds on. Routes
are plain `def`, so FastAPI runs them in its threadpool: board_data and the graph
build read sqlite and session stores and never block the event loop.

What replaces the daemon's own request checks (crew_graph_serve.Handler):
  - Host allowlist (421) and the tailnet-login check: the Hermes server's host-header
    middleware and its auth middleware (session token / OAuth gate) run before any
    route here; there is no second listener to protect.
  - _same_origin on POST /ack: the daemon needs it because a browser sends its ambient
    cookies to any origin. Here a write must carry the dashboard session token header
    (the tab's authedFetch, Desktop's ctx.rest) or the OAuth session, which Hermes's auth
    middleware checks first; a cross-site page cannot set that header. The route also
    takes POST only (GET answers 405), validates the action name, and a refused write
    never reaches the ack file.
"""

from __future__ import annotations

import base64
import html
import importlib.util
import json
import logging
import os
import re
import secrets
import sys
import threading
import urllib.parse
from fastapi import APIRouter, HTTPException, Request, Response

_log = logging.getLogger(__name__)

router = APIRouter()

PREFIX = "/api/plugins/crew/"
# crew's scripts sit next to this folder, in the dev tree and in an installed plugins/crew/.
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")

_VALID_HOST_RE = re.compile(r"^[a-zA-Z0-9.-]+(?::[0-9]{1,5})?$")
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
# A card id, optionally with the .json suffix the card page polls (card/<id>.json).
_CARD_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+(?:\.json)?$")
_ACK_PATH_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_AVATAR_PATH_RE = re.compile(r"^[a-zA-Z0-9_/-]+\.(?:svg|png)$")
_NONCE_RE = re.compile(r"'nonce-([A-Za-z0-9_+/=-]+)'")

# Headers every response carries (the daemon sent the same three).
_RESPONSE_HEADERS = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
}

_crew_lock = threading.Lock()
_crew_mod = None


def _crew():
    """crew_graph_serve, loaded once from this plugin's own scripts/ (never from a path on sys.path).

    It resolves HERMES_HOME and the kanban board through crew_card.kanban_db(), the one resolver, in this process's
    environment: where hermes_cli is importable (it is, inside the Hermes web server) that is Hermes's own
    kanban_db_path(), the same file the daemon's bare-interpreter fallback mirrors."""
    global _crew_mod
    with _crew_lock:
        if _crew_mod is None:
            path = os.path.join(SCRIPTS_DIR, "crew_graph_serve.py")
            loaded = sys.modules.get("crew_graph_serve")
            if loaded is not None and os.path.abspath(getattr(loaded, "__file__", "")) == os.path.abspath(path):
                _crew_mod = loaded
            else:
                if SCRIPTS_DIR not in sys.path:
                    sys.path.insert(0, SCRIPTS_DIR)   # crew_graph imports crew_card / crew_result by bare name
                spec = importlib.util.spec_from_file_location("crew_graph_serve", path)
                mod = importlib.util.module_from_spec(spec)
                sys.modules["crew_graph_serve"] = mod
                try:
                    spec.loader.exec_module(mod)
                except BaseException:
                    sys.modules.pop("crew_graph_serve", None)
                    raise
                _crew_mod = mod
        return _crew_mod


def _sanitize_color(val: str | None) -> str | None:
    if not val:
        return None
    val = val.strip()
    return val.lower() if _HEX_COLOR_RE.match(val) else None


def _public_crew_url(request: Request) -> str:
    """The standalone board URL for the "open in new tab" link, from the client's own host.

    Scheme is http or https only; the host must be a bare hostname[:port] (no path,
    quote, whitespace or userinfo) or the loopback default is used. Callers still
    html.escape the result before it goes into markup.
    """
    env = (os.environ.get("CREW_DASHBOARD_URL") or "").strip()
    if env and urllib.parse.urlsplit(env).scheme in ("http", "https") and not any(c in env for c in "\"'<> \t\r\n"):
        return env.rstrip("/")
    raw_proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    proto = "https" if str(raw_proto).strip().lower() == "https" else "http"
    raw_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "127.0.0.1:9119"
    raw_host = str(raw_host).strip()
    host = raw_host if _VALID_HOST_RE.match(raw_host) else "127.0.0.1:9119"
    return f"{proto}://{host}{PREFIX}board"


def _tab_csp(page_csp: str) -> str:
    """The page's nonce policy, with base-uri allowing the <base> the tab rewrite injects."""
    policy = re.sub(r"base-uri[^;]*", "base-uri 'self'", page_csp)
    return policy if "base-uri" in policy else policy.rstrip("; ") + "; base-uri 'self'"


# Runs first inside the proxied page. Only acts when the page is the tab's srcdoc
# iframe (same origin, parent exposes the plugin SDK); a page opened directly is
# left alone. Every request the page makes - fetch, XMLHttpRequest, avatar
# images - goes through the parent's authedFetch, and a click on a board/card
# link asks the tab (postMessage) to load that page, because a plain iframe
# navigation carries no session header; the card's open chat button asks it to open a chat session.
_BRIDGE_JS = r"""(function () {
  var parent = window.parent, sdk;
  try { sdk = parent !== window && parent.__HERMES_PLUGIN_SDK__; } catch (e) { sdk = null; }
  if (!sdk || !sdk.authedFetch) return;
  var origin = parent.location.origin;
  var basePath = new URL(document.baseURI).pathname;
  var hermesBase = parent.__HERMES_BASE_PATH__ || "";
  function rel(u) {
    var url = new URL(String(u), document.baseURI);
    if (url.origin !== origin) return null;
    var p = url.pathname;
    if (p.indexOf(basePath) === 0) p = p.slice(basePath.length);
    else if (p.indexOf(hermesBase + "/api/") === 0) return null;
    else p = p.replace(/^\/+/, "");
    if (p === "" || p === "index.html") p = "board";
    return { path: p, search: url.search };
  }
  function crewFetch(u, init) {
    var r = rel(u);
    if (!r) return Promise.reject(new Error("crew: request outside the plugin: " + u));
    return sdk.authedFetch("/api/plugins/crew/" + r.path + r.search, init);
  }
  window.fetch = function (u, init) { return crewFetch(u && u.url ? u.url : u, init); };
  function FX() { this.readyState = 0; this.status = 0; this.responseText = ""; this._h = {}; }
  FX.prototype.open = function (m, u) { this._m = m; this._u = u; this.readyState = 1; };
  FX.prototype.setRequestHeader = function (k, v) { this._h[k] = v; };
  FX.prototype.abort = function () {};
  FX.prototype._done = function () {
    this.readyState = 4;
    if (this.onreadystatechange) this.onreadystatechange();
    if (this.status && this.onload) this.onload();
    if (!this.status && this.onerror) this.onerror();
  };
  FX.prototype.send = function (body) {
    var x = this;
    crewFetch(x._u, { method: x._m || "GET", headers: x._h, body: body == null ? undefined : body, cache: "no-store" })
      .then(function (r) { x.status = r.status; return r.text(); })
      .then(function (t) { x.responseText = t; x._done(); }, function () { x.status = 0; x._done(); });
  };
  window.XMLHttpRequest = FX;
  function nav(path) { parent.postMessage({ type: "crew:navigate", path: path }, origin); }
  window.__crewNav = function (u) { var r = rel(u); if (r) nav(r.path + r.search); };
  window.__crewOpenChat = function (profile, session) { parent.postMessage({ type: "crew:open-session", profile: String(profile), session: String(session) }, origin); };
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
    var href = a.getAttribute("href");
    if (href.charAt(0) === "#") return;
    var r = rel(href);
    if (!r || !/^(board|card\/[A-Za-z0-9_-]+)$/.test(r.path) || (r.search && r.search !== "?all=1")) return;
    e.preventDefault();
    nav(r.path + r.search);
  }, true);
  // Role faces: an <img src> load sends no session header, so the page's face URL (lib.js faceUrl, rewritten
  // by the page rewrite to call __crewFace) starts as an empty placeholder naming the face; the observer below
  // fetches it through authedFetch and swaps in a data: URL, cached per face.
  var faces = {}, faceData = {}, MARK = "#crew-face=";
  var BLANK = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E";
  window.__crewFace = function (p) { return faceData[p] || (BLANK + MARK + p); };
  function face(img) {
    var src = img.getAttribute("src") || "", path, at = src.indexOf(MARK);
    if (src.indexOf("data:") === 0) {
      if (at === -1) return;
      path = src.slice(at + MARK.length);
    } else {
      var r = rel(src);
      if (!r) return;
      path = r.path;
    }
    if (!/^avatars\/[A-Za-z0-9_\/-]+\.(svg|png)$/.test(path)) return;
    if (!faces[path]) {
      faces[path] = crewFetch(path).then(function (res) {
        if (!res.ok) throw new Error(res.status);
        return res.blob();
      }).then(function (b) {
        return new Promise(function (ok) { var fr = new FileReader(); fr.onload = function () { ok(fr.result); }; fr.readAsDataURL(b); });
      }).then(function (data) { faceData[path] = data; return data; });
    }
    faces[path].then(function (data) { if (img.getAttribute("src") === src) img.setAttribute("src", data); }, function () {});
  }
  function scan(root) {
    if (root.tagName === "IMG") face(root);
    if (root.querySelectorAll) Array.prototype.forEach.call(root.querySelectorAll("img[src]"), face);
  }
  new MutationObserver(function (ms) {
    ms.forEach(function (m) {
      if (m.type === "attributes") face(m.target);
      else Array.prototype.forEach.call(m.addedNodes, function (n) { if (n.nodeType === 1) scan(n); });
    });
  }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });
  document.addEventListener("DOMContentLoaded", function () { scan(document); });
})();"""


def _theme_style(request: Request) -> str:
    bg_param = _sanitize_color(request.query_params.get("bg"))
    fg_param = _sanitize_color(request.query_params.get("fg"))
    raw_theme = (request.query_params.get("theme") or "").strip().lower()
    theme_param = raw_theme if raw_theme in ("light", "dark") else None
    if not (bg_param or fg_param or theme_param):
        return ""
    theme_bg = bg_param or ("#ffffff" if theme_param == "light" else "#041c1c")
    theme_fg = fg_param or ("#17171a" if theme_param == "light" else "#ffffff")
    scheme = theme_param or ("light" if theme_bg.lower() in ("#fff", "#ffffff") else "dark")
    return f"""
<style id="hermes-theme-sync">
:root {{
  color-scheme: {scheme} !important;
  --crew-scheme: {scheme} !important;
  --crew-bg: {theme_bg} !important;
  --color-background: {theme_bg} !important;
  --crew-fg: {theme_fg} !important;
  --color-foreground: {theme_fg} !important;
}}
html, body, main#board, main, #main, .page-card {{
  background-color: {theme_bg} !important;
  color: {theme_fg} !important;
}}
</style>
"""


def _rewrite_html(raw_html: str, request: Request, csp: str | None) -> str:
    # Absolute page paths become relative so the injected <base> resolves them under /api/plugins/crew/.
    raw_html = raw_html.replace('href="/card/', 'href="card/')
    raw_html = raw_html.replace('fetch("/board.json', 'fetch("board.json')
    raw_html = raw_html.replace('fetch("/ack/', 'fetch("ack/')
    raw_html = raw_html.replace('"/card/', '"card/')
    raw_html = raw_html.replace('href="/"', 'href="board"')
    raw_html = raw_html.replace("href='/'", "href='board'")
    raw_html = raw_html.replace('"/avatars/', '"avatars/')
    raw_html = raw_html.replace("'/avatars/", "'avatars/")
    # lib.js faceUrl: inside the tab the bridge supplies the face (see __crewFace in _BRIDGE_JS).
    raw_html = raw_html.replace(
        'return "avatars/role/" + (n || "unknown") + ".svg"; }',
        'var p = "avatars/role/" + (n || "unknown") + ".svg"; return window.__crewFace ? window.__crewFace(p) : p; }',
    )
    # The card page's "o" shortcut: back to the board through the tab, not a bare iframe navigation.
    raw_html = raw_html.replace(
        'window.location.href = "/";',
        '(window.__crewNav ? window.__crewNav("board") : (window.location.href = "board"));',
    )

    # The header link: the standalone board on the client's own (validated, escaped) host.
    safe_url = html.escape(_public_crew_url(request), quote=True)

    def _replace_tailnet(_m):
        return (
            f'<a class="tailnet" href="{safe_url}" target="_blank" rel="noopener noreferrer" '
            'style="color: #34d399; text-decoration: underline; font-weight: 500;" '
            'title="Open standalone board in new tab">open in new tab ↗</a>'
        )

    raw_html = re.sub(r"<a class=tailnet[^>]*>.*?</a>", _replace_tailnet, raw_html)

    if "<head>" not in raw_html or "<base " in raw_html:
        return raw_html
    head = f'<head><base href="{PREFIX}">'
    nonce_attr = ""
    if csp:
        # A srcdoc document never sees response headers: carry the policy in a <meta> as well.
        # frame-ancestors is ignored (and warned about) in a <meta>, so it stays header-only.
        meta_policy = re.sub(r";?\s*frame-ancestors[^;]*", "", csp).strip("; ")
        # Inside srcdoc 'self' is the Hermes dashboard origin, so script-src 'self' would let
        # injected markup load any script the dashboard serves. Crew's scripts are all inline with
        # the nonce (dashboard_asset inlines lib.js/board.js): the meta script-src is nonce-only.
        meta_policy = re.sub(
            r"(script-src)([^;]*)",
            lambda m: m.group(1) + re.sub(r"\s+'self'(?=\s|$)", "", m.group(2)),
            meta_policy,
        )
        head += f'<meta http-equiv="Content-Security-Policy" content="{html.escape(meta_policy, quote=True)}">'
        m = _NONCE_RE.search(csp)
        if m:
            nonce_attr = f' nonce="{html.escape(m.group(1), quote=True)}"'
    head += _theme_style(request)
    head += f'<script id="crew-parent-bridge"{nonce_attr}>{_BRIDGE_JS}</script>'
    return raw_html.replace("<head>", head, 1)


# The Hermes Desktop embed. Desktop shows a page in SandboxedFrame: a data: document with an opaque origin, no
# cookies, no parent access, relative URLs that resolve to nothing. ctx.rest (the only authenticated door) answers
# JSON only, so the /embed/ routes below wrap each page, face and ack in a JSON envelope, and this script - put in
# the page by _embed_html and by nothing else - sends every fetch, XMLHttpRequest, card-link click and face load to
# the Desktop plugin (desktop/plugin.js) with postMessage, which checks the path against an allowlist and calls
# ctx.rest. Replies come back the same way; the frame trusts only messages from its own parent. No credential is
# ever in the frame. Outside Desktop (window.parent === window) the script returns at once.
_EMBED_BRIDGE_JS = r"""(function () {
  if (window.parent === window) return;
  var seq = 0, wait = {};
  // The frame has an opaque origin: the parent's origin cannot be named, so "*" is the only target.
  function send(m) { m.crew = 1; window.parent.postMessage(m, "*"); }
  window.addEventListener("message", function (e) {
    var m = e.data;
    if (e.source !== window.parent || !m || m.crew !== 1 || !wait[m.id]) return;
    var w = wait[m.id]; delete wait[m.id];
    if (m.error) w.no(new TypeError(String(m.error))); else w.ok(m);
  });
  function rel(u) {
    var s = String(u && u.url ? u.url : u);
    if (/^[a-z][a-z0-9+.-]*:/i.test(s)) return null;
    s = s.replace(/^\/+/, "");
    if (s === "" || s === "index.html") s = "board";
    else if (s.charAt(0) === "?") s = "board" + s;
    return s;
  }
  function call(method, u, body) {
    var path = rel(u);
    if (path === null) return Promise.reject(new TypeError("crew: request outside the page: " + u));
    return new Promise(function (ok, no) {
      var id = ++seq; wait[id] = { ok: ok, no: no };
      send({ id: id, method: method, path: path, body: body === undefined ? null : body });
    });
  }
  window.fetch = function (u, init) {
    return call((init && init.method) || "GET", u, init && init.body).then(function (r) {
      return new Response(r.text, { status: r.status });
    });
  };
  function FX() { this.readyState = 0; this.status = 0; this.responseText = ""; }
  FX.prototype.open = function (m, u) { this._m = m; this._u = u; this.readyState = 1; };
  FX.prototype.setRequestHeader = function () {};
  FX.prototype.abort = function () {};
  FX.prototype._done = function () {
    this.readyState = 4;
    if (this.onreadystatechange) this.onreadystatechange();
    if (this.status && this.onload) this.onload();
    if (!this.status && this.onerror) this.onerror();
  };
  FX.prototype.send = function (body) {
    var x = this;
    call(x._m || "GET", x._u, body == null ? undefined : body).then(function (r) { x.status = r.status; x.responseText = r.text; x._done(); },
                                                                      function () { x.status = 0; x._done(); });
  };
  window.XMLHttpRequest = FX;
  function nav(path) { send({ navigate: path }); }
  window.__crewNav = function (u) { var p = rel(u); if (p !== null) nav(p); };
  window.__crewOpenChat = function (profile, session) { send({ openSession: { profile: String(profile), session: String(session) } }); };
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var href = a.getAttribute("href");
    if (href.charAt(0) === "#") return;
    e.preventDefault();   // nothing in the frame may navigate it: the parent swaps the page
    var p = rel(href);
    if (p !== null && /^(board|card\/[A-Za-z0-9_-]+)(\?all=1)?$/.test(p)) nav(p);
  }, true);
  // Role faces: an <img src> cannot reach the parent, so lib.js faceUrl (rewritten by _embed_html to call
  // __crewFace) starts as an empty placeholder naming the face; the observer swaps in the data: URL the parent returns.
  var faces = {}, faceData = {}, MARK = "#crew-face=";
  var BLANK = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E";
  window.__crewFace = function (p) { return faceData[p] || (BLANK + MARK + p); };
  function face(img) {
    var src = img.getAttribute("src") || "", at = src.indexOf(MARK), path;
    if (at === -1) return;
    path = src.slice(at + MARK.length);
    if (!/^avatars\/role\/[a-z0-9_-]+\.svg$/.test(path)) return;
    if (!faces[path]) faces[path] = call("GET", path).then(function (r) { faceData[path] = r.text; return r.text; });
    faces[path].then(function (data) { if (img.getAttribute("src") === src) img.setAttribute("src", data); }, function () {});
  }
  function scan(root) {
    if (root.tagName === "IMG") face(root);
    if (root.querySelectorAll) Array.prototype.forEach.call(root.querySelectorAll("img[src]"), face);
  }
  new MutationObserver(function (ms) {
    ms.forEach(function (m) {
      if (m.type === "attributes") face(m.target);
      else Array.prototype.forEach.call(m.addedNodes, function (n) { if (n.nodeType === 1) scan(n); });
    });
  }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });
  document.addEventListener("DOMContentLoaded", function () { scan(document); });
})();"""


def _embed_html(raw_html: str, csp: str | None) -> tuple[str, str]:
    """(html, nonce) for a Desktop data: document: the page as the board builds it, with no <base>, and the embed bridge.

    The page keeps its absolute paths (/card/x, /board.json): the bridge reads them as strings, never as URLs.
    """
    raw_html = re.sub(r"<a class=tailnet[^>]*>.*?</a>", "", raw_html)   # a standalone-board link; a frame cannot open it
    raw_html = raw_html.replace(
        'return "/avatars/role/" + (n || "unknown") + ".svg"; }',
        'var p = "avatars/role/" + (n || "unknown") + ".svg"; return window.__crewFace ? window.__crewFace(p) : "/" + p; }',
    )
    raw_html = raw_html.replace(
        'window.location.href = "/";',
        '(window.__crewNav ? window.__crewNav("board") : (window.location.href = "/"));',
    )
    raw_html = re.sub(r"<base\b[^>]*>", "", raw_html)
    nonce = ""
    head = ""
    if csp:
        # a data: document never sees response headers: carry the policy in a <meta>, as the tab page does
        # (frame-ancestors is header-only; 'self' is dropped from script-src so only the nonce'd inline scripts run)
        meta_policy = re.sub(r";?\s*frame-ancestors[^;]*", "", csp).strip("; ")
        meta_policy = re.sub(
            r"(script-src)([^;]*)",
            lambda m: m.group(1) + re.sub(r"\s+'self'(?=\s|$)", "", m.group(2)),
            meta_policy,
        )
        head += f'<meta http-equiv="Content-Security-Policy" content="{html.escape(meta_policy, quote=True)}">'
        m = _NONCE_RE.search(csp)
        if m:
            nonce = m.group(1)
    nonce_attr = f' nonce="{html.escape(nonce, quote=True)}"' if nonce else ""
    head += f'<script id="crew-desktop-bridge"{nonce_attr}>{_EMBED_BRIDGE_JS}</script>'
    if "<head>" in raw_html:
        return raw_html.replace("<head>", "<head>" + head, 1), nonce
    return head + raw_html, nonce


def _text_envelope(status: int, content: bytes) -> Response:
    """An ack's plain-text answer as JSON, with its status inside: ctx.rest throws on a non-2xx, and a 409
    ("only a row on the list is cleared") is an answer the page shows, not a transport failure."""
    return Response(content=json.dumps({"ok": 200 <= status < 300, "status": status,
                                        "text": content.decode("utf-8", errors="replace")}),
                    status_code=200, media_type="application/json")


def _render(request: Request, make, embed: str | None = None) -> Response:
    """Build one response in-process. make(crew, nonce) -> (status, body, content type) calls crew's builders.

    embed (the /embed/ routes only): "page" returns {html, nonce}, "asset" {data_url}, "text" {ok, status, text};
    a non-200 passes through unchanged for every mode but "text". An exception is a 500 page (or, for "text", a
    500 inside the envelope) with the detail in the server log only: there is no second path to fall back to."""
    nonce = secrets.token_urlsafe(16)
    try:
        crew = _crew()
        status, body, content_type = make(crew, nonce)
        content = body.encode("utf-8") if isinstance(body, str) else body
        if status >= 400 and embed != "text":   # an error answer passes through as built, unrewritten (as the daemon's did)
            return Response(content=content, status_code=status, media_type=content_type)
        is_html = content_type.startswith("text/html")
        csp = _tab_csp(crew.csp(nonce)) if is_html else None
        res_headers = dict(_RESPONSE_HEADERS)
        if csp:
            res_headers["Content-Security-Policy"] = csp

        if embed == "page" and status == 200:
            page, page_nonce = _embed_html(content.decode("utf-8", errors="replace"), csp)
            return Response(content=json.dumps({"html": page, "nonce": page_nonce}), status_code=200,
                            headers={"Cache-Control": "no-store"}, media_type="application/json")
        if embed == "asset" and status == 200:
            mime = content_type.split(";")[0].strip()
            data_url = f"data:{mime};base64," + base64.b64encode(content).decode("ascii")
            return Response(content=json.dumps({"data_url": data_url}), status_code=200,
                            headers={"Cache-Control": "no-store"}, media_type="application/json")
        if embed == "text":
            return _text_envelope(status, content)

        if is_html:
            content = _rewrite_html(content.decode("utf-8", errors="replace"), request, csp).encode("utf-8")
        return Response(content=content, status_code=status, headers=res_headers, media_type=content_type)
    except Exception:
        _log.exception("crew dashboard: building %s failed", request.url.path)
        if embed == "text":
            return _text_envelope(500, b"crew could not build this answer; see the Hermes server log\n")
        return Response(
            content=("<html><body><h3>Crew Dashboard Unavailable</h3><p>Crew could not build this page. "
                     "See the Hermes server log for the error.</p></body></html>"),
            status_code=500,
            media_type="text/html",
        )


def _flag(request: Request, name: str) -> bool:
    return request.query_params.get(name) == "1"


def _board_page(request: Request):
    show_all = _flag(request, "all")
    return lambda crew, nonce: (200, crew.board_page(show_all, nonce), "text/html; charset=utf-8")


def _board_json(request: Request):
    show_all, older = _flag(request, "all"), _flag(request, "older")
    return lambda crew, nonce: (200, crew.board_json(show_all, older), "application/json")


def _card(card_id: str):
    def make(crew, nonce):
        return crew.card_response(card_id, nonce)
    return make


def _avatar(avatar_path: str):
    def make(crew, nonce):
        m = crew.FACE_RX.match("/avatars/" + avatar_path)
        svg = crew.face_svg(m.group(1)) if m else None
        if svg is None:
            return 404, "not found\n", "text/plain; charset=utf-8"
        return 200, svg, "image/svg+xml"
    return make


def _ack(action: str, undo: bool):
    def make(crew, nonce):
        code, text = crew.ack_all(undo=undo) if action == "all" else crew.ack_card(action, undo=undo)
        return code, text, "text/plain; charset=utf-8"
    return make


def _health(crew, nonce):
    try:
        board = crew.board_data()
        body = "ok crew board cards=%s live=%s\n" % (board.get("cards"), board.get("live"))
    except Exception:
        _log.exception("crew dashboard: health could not read the board")
        body = "ok crew board (no data; see the Hermes server log)\n"
    return 200, body, "text/plain; charset=utf-8"


@router.get("/board")
def get_board(request: Request):
    return _render(request, _board_page(request))


@router.get("/board.json")
def get_board_json(request: Request):
    return _render(request, _board_json(request))


@router.get("/card/{card_id}")
def get_card(card_id: str, request: Request):
    if not _CARD_ID_RE.match(card_id):
        raise HTTPException(status_code=400, detail="Invalid card ID format")
    return _render(request, _card(card_id))


@router.post("/ack/{action}")
def post_ack(action: str, request: Request):
    if not _ACK_PATH_RE.match(action):
        raise HTTPException(status_code=400, detail="Invalid ack target format")
    return _render(request, _ack(action, _flag(request, "undo")))


@router.get("/avatars/{avatar_path:path}")
def get_avatars(avatar_path: str, request: Request):
    if ".." in avatar_path or not _AVATAR_PATH_RE.match(avatar_path):
        raise HTTPException(status_code=400, detail="Invalid avatar asset path")
    return _render(request, _avatar(avatar_path))


@router.get("/healthz")
def get_healthz(request: Request):
    return _render(request, _health)


# --- Hermes Desktop embed (desktop/plugin.js): the same pages, wrapped in JSON because ctx.rest answers JSON only.
# Ids and names are validated exactly like the routes above.
@router.get("/embed/board")
def get_embed_board(request: Request):
    return _render(request, _board_page(request), embed="page")


@router.get("/embed/card/{card_id}")
def get_embed_card(card_id: str, request: Request):
    if not _CARD_ID_RE.match(card_id) or card_id.endswith(".json"):
        raise HTTPException(status_code=400, detail="Invalid card ID format")
    return _render(request, _card(card_id), embed="page")


@router.get("/embed/avatar/{avatar_path:path}")
def get_embed_avatar(avatar_path: str, request: Request):
    if ".." in avatar_path or not _AVATAR_PATH_RE.match(avatar_path):
        raise HTTPException(status_code=400, detail="Invalid avatar asset path")
    return _render(request, _avatar(avatar_path), embed="asset")


@router.post("/embed/ack/{action}")
def post_embed_ack(action: str, request: Request):
    if not _ACK_PATH_RE.match(action):
        raise HTTPException(status_code=400, detail="Invalid ack target format")
    return _render(request, _ack(action, _flag(request, "undo")), embed="text")
