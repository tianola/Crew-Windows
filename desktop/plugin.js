// Crew page for Hermes Desktop: our own board and card pages, unchanged, in a sandboxed frame.
//
// Plain ESM, no JSX, only @hermes/plugin-sdk / react / react/jsx-runtime (the Desktop disk-plugin contract).
// The pages come from dashboard/plugin_api.py's /embed/ routes, which wrap each page in JSON because ctx.rest
// answers JSON only. The frame is a data: document with an opaque origin: it cannot fetch, hold a credential or
// reach the app. Its bridge script (_EMBED_BRIDGE_JS in plugin_api.py) sends every request as
// postMessage({crew: 1, id, method, path, body}); handleFrameMessage checks who sent it and what it asks for,
// calls ctx.rest, and answers the same way.

import { ROUTES_AREA, SIDEBAR_NAV_AREA, SandboxedFrame } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useRef, useState } from 'react'

const ID = '[A-Za-z0-9_-]+'
const BOARD_QUERY = '(\\?(?:all=1|older=1)(?:&(?:all=1|older=1))?)?'

// The only requests the frame may make, by the path it names (no leading slash, query included) -> the route
// under /api/plugins/crew/ that ctx.rest calls. Ids and names cannot hold a dot or a slash, so no ".." gets through.
const ROUTES = [
  { method: 'GET', re: new RegExp('^board\\.json' + BOARD_QUERY + '$'), to: m => '/board.json' + (m[1] || ''), out: 'json' },
  { method: 'GET', re: new RegExp('^card/(' + ID + ')\\.json$'), to: m => '/card/' + m[1] + '.json', out: 'json' },
  { method: 'GET', re: /^avatars\/role\/([a-z0-9][a-z0-9_-]{0,39})\.svg$/, to: m => '/embed/avatar/role/' + m[1] + '.svg', out: 'data_url' },
  { method: 'POST', re: new RegExp('^ack/(' + ID + ')(\\?undo=1)?$'), to: m => '/embed/ack/' + m[1] + (m[2] || ''), out: 'ack' }
]

// The pages a card link or the "o" shortcut may open.
const NAV_RE = new RegExp('^(?:(board)|card/(' + ID + '))(\\?all=1)?$')

export function routeFor(method, path) {
  if (typeof method !== 'string' || typeof path !== 'string') return null
  for (const r of ROUTES) {
    const m = r.method === method.toUpperCase() ? r.re.exec(path) : null
    if (m) return { path: r.to(m), out: r.out }
  }
  return null
}

// A chat session the card page may ask to open: a profile name and a session id.
const PROFILE_RE = /^[A-Za-z0-9_.-]{1,64}$/
const SESSION_RE = /^[A-Za-z0-9_-]{1,128}$/

export function openSessionTarget(v) {
  if (!v || typeof v !== 'object' || typeof v.profile !== 'string' || typeof v.session !== 'string') return null
  if (v.session === 'no-session' || !PROFILE_RE.test(v.profile) || !SESSION_RE.test(v.session)) return null
  return { profile: v.profile, session: v.session }
}

export function navTarget(path) {
  const m = typeof path === 'string' ? NAV_RE.exec(path) : null
  return m ? { page: m[1] ? 'board' : 'card', id: m[2] || null, all: !!m[3] } : null
}

function embedPath(view) {
  const q = view.all ? '?all=1' : ''
  return view.page === 'card' ? '/embed/card/' + view.id + q : '/embed/board' + q
}

// SandboxedFrame takes http(s): or data: only, and a data: URL is never resolved against anything. UTF-8 -> base64.
export function toDataUrl(html) {
  const bytes = new TextEncoder().encode(html)
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000))
  return 'data:text/html;charset=utf-8;base64,' + btoa(bin)
}

function reply(source, id, body) {
  // The frame's origin is opaque ("null"): it has no origin name a targetOrigin could match, so "*" is the only
  // working target. The reply goes to event.source only, and holds nothing but the page's own data.
  try { source.postMessage({ crew: 1, id, ...body }, '*') } catch (e) { /* the frame went away */ }
}

// One message from the frame. frameWindow is the current frame's contentWindow: anything else is ignored.
export async function handleFrameMessage(ctx, frameWindow, event, setView) {
  const m = event.data
  if (!frameWindow || event.source !== frameWindow || !m || m.crew !== 1) return
  if (m.navigate !== undefined) {
    const view = navTarget(m.navigate)
    if (view) setView(view)
    return
  }
  if (m.openSession !== undefined) {
    // The Desktop plugin ctx has no session-open call. window.hermesDesktop.openSessionWindow is the preload API
    // (apps/desktop/electron/preload.ts:55); it opens the session in its own window.
    const t = openSessionTarget(m.openSession)
    const open = globalThis.window && window.hermesDesktop && window.hermesDesktop.openSessionWindow
    if (t && typeof open === 'function') open(t.session, { profile: t.profile })
    return
  }
  const id = m.id
  if (typeof id !== 'number') return
  const route = routeFor(m.method, m.path)
  if (!route) return reply(event.source, id, { error: 'crew: path not allowed' })
  try {
    const res = await ctx.rest(route.path, { method: m.method.toUpperCase() })
    if (route.out === 'json') reply(event.source, id, { status: 200, text: JSON.stringify(res) })
    else if (route.out === 'data_url') reply(event.source, id, { status: 200, text: res.data_url })
    else reply(event.source, id, { status: res.status, text: res.text })
  } catch (e) {
    reply(event.source, id, { error: String((e && e.message) || e) })
  }
}

function CrewPage({ ctx }) {
  const [view, setView] = useState({ page: 'board', id: null, all: false })
  const [src, setSrc] = useState(null)
  const [error, setError] = useState(null)
  const frameRef = useRef(null)

  useEffect(() => {
    let live = true
    setSrc(null)
    setError(null)
    ctx.rest(embedPath(view))
      .then(env => { if (live) setSrc(toDataUrl(env.html)) })
      .catch(e => { if (live) setError(String((e && e.message) || e)) })
    return () => { live = false }
  }, [view])

  useEffect(() => ctx.addEventListener(window, 'message', event =>
    handleFrameMessage(ctx, frameRef.current && frameRef.current.contentWindow, event, setView)), [])

  return jsxs('div', {
    className: 'flex h-full min-h-0 w-full flex-col',
    // No bar above the page: the card page carries its own link back to the board.
    children: [
      error
        ? jsx('div', { className: 'p-4 text-sm text-(--ui-text-tertiary)', children: 'Crew could not load: ' + error })
        : src
          ? jsx(SandboxedFrame, { ref: frameRef, src, title: 'Crew', className: 'h-full min-h-0 w-full flex-1 border-0' })
          : null
    ]
  })
}

export default {
  id: 'crew',
  name: 'Crew',
  register(ctx) {
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/crew' }, render: () => jsx(CrewPage, { ctx }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/crew', label: 'Crew', codicon: 'organization' } }
    ])
  }
}
