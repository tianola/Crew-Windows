"""Dashboard plugin routes (dashboard/plugin_api.py) driven through FastAPI's TestClient.

The pages are built in-process by crew's own builders, so every case runs against a real temp kanban board and the
real crew_graph_serve functions: no daemon, nothing listening. Covers the PR #3 review: the escaped public host,
the allowlisted routes, the 401 a caller without the dashboard session token gets from Hermes's own auth
middleware, the CSP + nonce on the page the tab renders via srcdoc, and an /ack write on a temp ack file.
"""
import html
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
CREW_ROOT = os.path.dirname(HERE)
for p in (CREW_ROOT, os.path.join(CREW_ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dashboard import plugin_api  # noqa: E402
from dashboard.plugin_api import (  # noqa: E402
    router,
    _sanitize_color,
    _public_crew_url,
    _CARD_ID_RE,
    _ACK_PATH_RE,
    _AVATAR_PATH_RE,
)

from tests import kernel_board as kbd  # noqa: E402


def _load_hermes_auth():
    """Hermes's real web_server auth middleware, imported under a throwaway HERMES_HOME.

    None when hermes-agent is not installed (the crew suite also runs without it).
    """
    saved = {k: os.environ.get(k) for k in ("HERMES_HOME", "HERMES_DASHBOARD_SESSION_TOKEN")}
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="crew-dash-home-")
    os.environ["HERMES_DASHBOARD_SESSION_TOKEN"] = "crew-test-session-token"
    try:
        import hermes_cli.web_server as ws
    except Exception:
        return None
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if not hasattr(ws, "auth_middleware") or not hasattr(ws, "_SESSION_TOKEN"):
        return None
    return ws


class RealBoardCase(unittest.TestCase):
    """A temp HERMES_HOME with a real kanban board (one card, one PROBE card) and its own ack file: what the
    in-process builders read. Nothing here listens on a port."""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="crew-dash-home-")
        keys = ("HERMES_HOME", "CREW_ACK_FILE", "HERMES_BIN", "KANBAN_DB", "HERMES_KANBAN_DB")
        cls._env = {k: os.environ.get(k) for k in keys}
        os.environ["HERMES_HOME"] = cls.home
        os.environ["CREW_ACK_FILE"] = os.path.join(cls.home, "crew", "attention_acks.json")
        os.environ.setdefault("HERMES_BIN", "/bin/false")
        os.environ.pop("HERMES_KANBAN_DB", None)
        kb, cls.conn, db_path = kbd.open_board(cls.home)
        os.environ["KANBAN_DB"] = db_path
        cls.card = kbd.add_card(cls.conn, title="dash card")
        cls.probe = kbd.add_card(cls.conn, title="PROBE scaffolding")
        cls.blocked = kbd.add_card(cls.conn, status="blocked", title="needs the owner")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(cls.home, ignore_errors=True)


class CrewDashboardCustomizationTests(RealBoardCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/plugins/crew")
        self.client = TestClient(self.app)

    def test_manifest_metadata(self):
        manifest_path = os.path.join(CREW_ROOT, "dashboard", "manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("name"), "crew")
        self.assertEqual(data.get("tab", {}).get("path"), "/crew")
        self.assertEqual(data.get("entry"), "dist/index.js")
        self.assertEqual(data.get("api"), "plugin_api.py")
        # The tab ships with the plugin it belongs to: one version for both.
        with open(os.path.join(CREW_ROOT, "plugin.yaml"), "r", encoding="utf-8") as f:
            plugin_version = next(l.split(":", 1)[1].strip() for l in f if l.startswith("version:"))
        self.assertEqual(data.get("version"), plugin_version)

    def test_dist_index_js_never_holds_a_credential(self):
        with open(os.path.join(CREW_ROOT, "dashboard", "dist", "index.js"), "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Hand-written ES5", content)
        for leak in ("_SESSION_TOKEN", "__HERMES_SESSION_TOKEN__", "X-Hermes-Session-Token", "token="):
            self.assertNotIn(leak, content)

    def test_color_sanitization_rejects_xss(self):
        self.assertEqual(_sanitize_color("#041c1c"), "#041c1c")
        self.assertEqual(_sanitize_color("#fff"), "#fff")
        self.assertEqual(_sanitize_color("#AABBCC"), "#aabbcc")
        for bad in ("</style><script>alert(1)</script>", "#041c1c; alert(1)", "red; background: blue",
                    "expression(alert(1))", "rgba(0,0,0,1)"):
            self.assertIsNone(_sanitize_color(bad), bad)

    def test_public_crew_url_sanitization(self):
        ok = MagicMock()
        ok.headers = {"host": "agent.example.com", "x-forwarded-proto": "https"}
        ok.url.scheme = "https"
        self.assertEqual(_public_crew_url(ok), "https://agent.example.com/api/plugins/crew/board")

        for host, proto in (('attacker.com" onclick="alert(1)', "javascript:alert(1)"),
                            ("evil.com/path", "http"), ("evil.com\\x", "http"), ("a b", "http"),
                            ("user@evil.com", "http"), ("evil.com'><script>", "https ")):
            hostile = MagicMock()
            hostile.headers = {"host": host, "x-forwarded-proto": proto}
            hostile.url.scheme = "http"
            url = _public_crew_url(hostile)
            self.assertRegex(url, r"^https?://127\.0\.0\.1:9119/api/plugins/crew/board$", host)

    def test_route_allowlisting_and_validation(self):
        self.assertTrue(_CARD_ID_RE.match("t_12345"))
        self.assertTrue(_CARD_ID_RE.match("card-abc_12"))
        self.assertTrue(_CARD_ID_RE.match("t_12345.json"))
        for bad in ("../../etc/passwd", "card/subpath", "t_1.json.json", "t_1.html", ".json"):
            self.assertFalse(_CARD_ID_RE.match(bad), bad)
        self.assertTrue(_ACK_PATH_RE.match("all"))
        self.assertTrue(_ACK_PATH_RE.match("t_12345"))
        self.assertFalse(_ACK_PATH_RE.match("all/extra"))
        self.assertTrue(_AVATAR_PATH_RE.match("role/worker.svg"))
        self.assertTrue(_AVATAR_PATH_RE.match("role/verifier.png"))
        self.assertFalse(_AVATAR_PATH_RE.match("../secret.txt"))
        self.assertFalse(_AVATAR_PATH_RE.match("role/worker.exe"))

    def test_only_allowlisted_routes_build_anything(self):
        with patch.object(plugin_api, "_crew") as crew:
            for path in ("/api/plugins/crew/index.json", "/api/plugins/crew/scripts/crew_card.py",
                         "/api/plugins/crew/card/t_1/extra", "/api/plugins/crew/avatars/role/../../x.svg",
                         "/api/plugins/crew/card/t_1%3Fall%3D1", "/api/plugins/crew/card/..%2F..%2Fx"):
                self.assertIn(self.client.get(path).status_code, (400, 404, 405), path)
            self.assertEqual(self.client.get("/api/plugins/crew/ack/all").status_code, 405)   # writes are POST only
            crew.assert_not_called()

    def test_card_json_poll_is_the_real_graph(self):
        resp = self.client.get("/api/plugins/crew/card/%s.json" % self.card)
        self.assertEqual(resp.status_code, 200, resp.text)
        import crew_graph_serve
        code, body, ctype = crew_graph_serve.card_response(self.card + ".json", "n")
        self.assertEqual((code, ctype), (200, "application/json"))
        mine, theirs = resp.json(), json.loads(body)
        for d in (mine, theirs):
            for k in [k for k in d if k.startswith("generated")]:
                d.pop(k)
        self.assertEqual(mine, theirs)
        self.assertEqual(self.client.get("/api/plugins/crew/card/t_nosuchcard.json").status_code, 404)

    def test_query_flags_reach_the_builders_and_credentials_do_not(self):
        import crew_graph_serve
        seen = []
        real = crew_graph_serve.board_json
        def spy(show_all=False, older=False):
            seen.append((show_all, older))
            return real(show_all, older)
        with patch.object(crew_graph_serve, "board_json", spy):
            self.client.get("/api/plugins/crew/board.json?all=1&older=1&token=secret&x=%2F..",
                            headers={"X-Hermes-Session-Token": "secret", "Authorization": "Bearer secret"})
            self.client.get("/api/plugins/crew/board.json?all=10")
            self.client.get("/api/plugins/crew/board.json")
        self.assertEqual(seen, [(True, True), (False, False), (False, False)])
        # anchor: ?all=1 lists the PROBE card, the default board hides it (board_data's own rule)
        plain = self.client.get("/api/plugins/crew/board.json").json()
        full = self.client.get("/api/plugins/crew/board.json?all=1").json()
        self.assertEqual(full["cards"], plain["cards"] + 1)
        self.assertNotIn("secret", json.dumps(plain))

    def test_board_page_escapes_host_and_keeps_the_nonce_csp(self):
        resp = self.client.get(
            "/api/plugins/crew/board?bg=%3C/style%3E%3Cscript%3Ealert(1)%3C/script%3E",
            headers={"X-Forwarded-Host": 'evil.com" onmouseover="alert(2)', "X-Forwarded-Proto": "javascript"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertNotIn("alert(1)", body)
        self.assertNotIn("alert(2)", body)
        self.assertNotIn("javascript", body.split("crew-parent-bridge")[0])
        self.assertIn('href="http://127.0.0.1:9119/api/plugins/crew/board" target="_blank"', body)
        self.assertNotIn("set-cookie", resp.headers)
        self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")

        # Header CSP: the page's nonce policy, with base-uri opened for the injected <base>.
        csp = resp.headers.get("content-security-policy")
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        self.assertIn("base-uri 'self'", csp)
        self.assertNotIn("base-uri 'none'", csp)
        self.assertIn("script-src 'self' 'nonce-%s'" % nonce, csp)
        # srcdoc never sees response headers: the same policy rides in a <meta>, minus frame-ancestors.
        self.assertIn('<meta http-equiv="Content-Security-Policy" content="default-src', body)
        self.assertNotIn("frame-ancestors", body)
        # Inside srcdoc 'self' is the dashboard origin: the meta policy's script-src is nonce-only.
        meta_m = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"', body)
        self.assertIsNotNone(meta_m)
        meta_policy = html.unescape(meta_m.group(1))
        script_src = re.search(r"script-src[^;]*", meta_policy).group(0)
        self.assertEqual(script_src.split(), ["script-src", "'nonce-%s'" % nonce])
        self.assertIn("default-src 'self'", meta_policy)
        self.assertIn("connect-src 'self'", meta_policy)
        # The injected bridge and the page's own script carry the response's nonce, so the policy lets them run.
        self.assertIn('<script id="crew-parent-bridge" nonce="%s">' % nonce, body)
        self.assertIn('<script nonce="%s">' % nonce, body)
        self.assertLess(body.index('<base href="/api/plugins/crew/">'), body.index("crew-parent-bridge"))
        # Root-absolute links are made relative to the <base>; the real board.js polls board.json.
        self.assertIn('fetch("board.json', body)
        self.assertNotIn('fetch("/board.json', body)
        self.assertIn('title="Open standalone board in new tab"', body)
        # An <img> load carries no session header: inside the tab the bridge supplies the role face.
        self.assertIn('return window.__crewFace ? window.__crewFace(p) : p; }', body)
        # a fresh nonce per response
        again = self.client.get("/api/plugins/crew/board").headers["content-security-policy"]
        self.assertNotEqual(re.search(r"'nonce-([^']+)'", again).group(1), nonce)

    def test_card_page_in_the_tab_links_back_through_the_base(self):
        resp = self.client.get("/api/plugins/crew/card/" + self.card)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('<base href="/api/plugins/crew/">', resp.text)
        self.assertIn('window.__crewNav ? window.__crewNav("board")', resp.text)
        self.assertIn("window.__crewOpenChat = function (profile, session)", resp.text)   # the bridge
        self.assertIn("cardCoOpen", resp.text)                                            # the card script
        missing = self.client.get("/api/plugins/crew/card/t_nosuchcard")
        self.assertEqual(missing.status_code, 404)
        self.assertIn("no graph for t_nosuchcard", missing.text)

    def test_a_failing_builder_answers_500_with_the_detail_in_the_log_only(self):
        import crew_graph_serve
        with patch.object(crew_graph_serve, "board_data", side_effect=OSError("<script>x</script> /secret/path")):
            with self.assertLogs(plugin_api._log, "ERROR"):
                resp = self.client.get("/api/plugins/crew/board.json")
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("<script>x", resp.text)
        self.assertNotIn("/secret/path", resp.text)

    def test_nothing_listens_and_nothing_is_fetched(self):
        # CREW_LOCAL_UPSTREAM is gone: pointing it at a closed port changes nothing, and urllib is never called.
        with patch.dict(os.environ, {"CREW_LOCAL_UPSTREAM": "http://127.0.0.1:9"}):
            with patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                for path in ("/board", "/board.json", "/card/" + self.card, "/card/%s.json" % self.card,
                             "/avatars/role/worker.svg", "/healthz", "/embed/board", "/embed/card/" + self.card,
                             "/embed/avatar/role/worker.svg"):
                    self.assertEqual(self.client.get("/api/plugins/crew" + path).status_code, 200, path)
                self.assertEqual(self.client.post("/api/plugins/crew/embed/ack/all?undo=1").status_code, 200)
        self.assertFalse(hasattr(plugin_api, "UPSTREAM"))

    def test_healthz(self):
        resp = self.client.get("/api/plugins/crew/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.text.startswith("ok crew board cards="))

    def test_healthz_never_puts_the_exception_text_in_the_answer(self):
        crew = plugin_api._crew()
        with patch.object(crew, "board_data", side_effect=RuntimeError("secret-path /home/x/kanban.db locked")):
            resp = self.client.get("/api/plugins/crew/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.text.startswith("ok crew board (no data"))
        self.assertNotIn("secret-path", resp.text)


@unittest.skipUnless(_load_hermes_auth(), "hermes-agent is not installed")
class HermesTokenAuthTests(RealBoardCase):
    """The plugin routes behind Hermes's real auth_middleware, as the dashboard mounts them."""

    def setUp(self):
        ws = _load_hermes_auth()
        self.token = ws._SESSION_TOKEN
        self.app = FastAPI()
        self.app.state.startup_ready = True
        self.app.middleware("http")(ws.auth_middleware)
        self.app.include_router(router, prefix="/api/plugins/crew")
        self.client = TestClient(self.app)

    def test_no_session_token_is_401_and_builds_nothing(self):
        with patch.object(plugin_api, "_crew") as crew:
            for path in ("/api/plugins/crew/board", "/api/plugins/crew/card/t_1", "/api/plugins/crew/card/t_1.json",
                         "/api/plugins/crew/board.json", "/api/plugins/crew/avatars/role/worker.svg",
                         "/api/plugins/crew/embed/board"):
                self.assertEqual(self.client.get(path).status_code, 401, path)
            self.assertEqual(self.client.post("/api/plugins/crew/ack/all").status_code, 401)
            self.assertEqual(self.client.post("/api/plugins/crew/embed/ack/all").status_code, 401)
            self.assertEqual(self.client.get("/api/plugins/crew/board",
                                             headers={"X-Hermes-Session-Token": "wrong"}).status_code, 401)
            crew.assert_not_called()

    def test_session_header_as_sent_by_authed_fetch_is_served(self):
        resp = self.client.get("/api/plugins/crew/board", headers={"X-Hermes-Session-Token": self.token})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="crew-parent-bridge"', resp.text)
        self.assertNotIn(self.token, resp.text)


class InProcessAckTests(RealBoardCase):
    """/ack on a temp board and a temp ack file (CREW_ACK_FILE): never the owner's."""

    def setUp(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/plugins/crew")
        self.client = TestClient(app)
        self.acks = os.environ["CREW_ACK_FILE"]
        if os.path.exists(self.acks):
            os.remove(self.acks)

    def test_ack_clears_one_row_and_undo_restores_it(self):
        base = "/api/plugins/crew/ack/"
        board = self.client.get("/api/plugins/crew/board.json").json()
        before = board["attention"]["total"]
        self.assertIn(self.blocked, [r["id"] for r in board["attention"]["rows"]])
        resp = self.client.post(base + self.blocked)
        self.assertEqual((resp.status_code, resp.text), (200, "cleared %s\n" % self.blocked))
        self.assertTrue(resp.headers["content-type"].startswith("text/plain"))
        after = self.client.get("/api/plugins/crew/board.json").json()["attention"]
        self.assertEqual(after["total"], before - 1)
        self.assertNotIn(self.blocked, [r["id"] for r in after["rows"]])
        with open(self.acks, encoding="utf-8") as fh:
            self.assertIn(self.blocked, json.load(fh))
        self.assertEqual(self.client.post(base + self.blocked + "?undo=1").text, "restored %s\n" % self.blocked)
        self.assertEqual(self.client.get("/api/plugins/crew/board.json").json()["attention"]["total"], before)

    def test_ack_semantics_match_the_daemon(self):
        base = "/api/plugins/crew/ack/"
        self.assertEqual(self.client.post(base + "t_nosuchcard").status_code, 404)
        busy = self.client.post(base + self.card)   # a ready card is not on the list
        self.assertEqual(busy.status_code, 409)
        self.assertIn("only a row on the list is cleared", busy.text)
        every = self.client.post(base + "all")
        self.assertEqual(every.status_code, 200)
        self.assertRegex(every.text, r"^cleared \d+ row\(s\)\n$")
        self.assertEqual(self.client.get("/api/plugins/crew/board.json").json()["attention"]["total"], 0)
        self.assertRegex(self.client.post(base + "all?undo=1").text, r"^restored \d+ row\(s\)\n$")
        self.assertEqual(self.client.post(base + "a%2Fb").status_code, 404)


if __name__ == "__main__":
    unittest.main()
