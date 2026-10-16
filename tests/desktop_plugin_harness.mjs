// Node harness for desktop/plugin.js: stubs the three modules a Desktop disk plugin may import, loads the real
// file, and checks registration, the request allowlist, the sender check and the page it hands to the frame.
// Usage: node desktop_plugin_harness.mjs <plugin.js> <scratch dir>   (exit 0 and "OK n checks" when all pass)
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import assert from 'node:assert/strict'

const [, , pluginPath, tmp] = process.argv
fs.writeFileSync(path.join(tmp, 'sdk.mjs'), `
export const ROUTES_AREA = 'routes', SIDEBAR_NAV_AREA = 'sidebar.nav'
export const Button = 'Button', SandboxedFrame = 'SandboxedFrame'
`)
fs.writeFileSync(path.join(tmp, 'jsx.mjs'), `
export const jsx = (type, props) => ({ type, props })
export const jsxs = jsx
`)
// one-render hook shim: state lives in globalThis.__hooks, effects are recorded for the test to run
fs.writeFileSync(path.join(tmp, 'react.mjs'), `
export const useState = init => { const h = globalThis.__hooks; const i = h.n++; if (!(i in h.state)) h.state[i] = init;
  return [h.state[i], v => { h.state[i] = v; h.sets.push(v) }] }
export const useRef = () => { const h = globalThis.__hooks; const i = h.n++; return h.state[i] || (h.state[i] = { current: null }) }
export const useEffect = (fn, deps) => { globalThis.__hooks.effects.push(fn) }
`)
const src = fs.readFileSync(pluginPath, 'utf8')
  .replace("'@hermes/plugin-sdk'", "'./sdk.mjs'").replace("'react/jsx-runtime'", "'./jsx.mjs'").replace("'react'", "'./react.mjs'")
assert.ok(!/from '(?!\.\/)/.test(src), 'plugin.js imports something other than the three allowed specifiers')
fs.writeFileSync(path.join(tmp, 'plugin.mjs'), src)
const mod = await import(pathToFileURL(path.join(tmp, 'plugin.mjs')).href)   // a bare C:\\ path is not a valid ESM specifier
const plugin = mod.default
let checks = 0
const ok = (cond, msg) => { assert.ok(cond, msg); checks++ }
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++ }

// --- registration
const calls = []
const rests = []
const listeners = []
const ctx = {
  registerMany: cs => calls.push(...cs),
  addEventListener: (target, type, fn) => { listeners.push({ target, type, fn }); return () => {} },
  rest: async (p, o) => { rests.push([p, o && o.method]); return ctx.answer(p) },
  answer: () => ({})
}
eq([plugin.id, typeof plugin.register], ['crew', 'function'])
plugin.register(ctx)
const page = calls.find(c => c.area === 'routes'), nav = calls.find(c => c.area === 'sidebar.nav')
eq(page.data, { path: '/crew' })
eq([nav.data.path, nav.data.label], ['/crew', 'Crew'])

// --- allowlist: the real paths the pages use are accepted and mapped, everything else is refused
const R = mod.routeFor
eq(R('GET', 'board.json'), { path: '/board.json', out: 'json' })
eq(R('GET', 'board.json?all=1&older=1').path, '/board.json?all=1&older=1')
eq(R('GET', 'card/t_ab12-C.json'), { path: '/card/t_ab12-C.json', out: 'json' })
eq(R('GET', 'avatars/role/worker.svg'), { path: '/embed/avatar/role/worker.svg', out: 'data_url' })
eq(R('POST', 'ack/all?undo=1'), { path: '/embed/ack/all?undo=1', out: 'ack' })
eq(R('post', 'ack/t_12'), { path: '/embed/ack/t_12', out: 'ack' })
for (const [m, p] of [
  ['GET', '../board.json'], ['GET', 'card/../../x.json'], ['GET', 'card/a/b.json'], ['GET', 'card/t_1.json?x=1'],
  ['GET', '/board.json'], ['GET', 'board.json?all=2'], ['GET', 'board.json?all=1&evil=1'], ['GET', 'board'],
  ['GET', 'embed/board'], ['GET', 'avatars/role/../../x.svg'], ['GET', 'avatars/other/x.svg'], ['GET', 'avatars/role/a.png'],
  ['GET', 'ack/all'], ['POST', 'board.json'], ['POST', 'ack/a/b'], ['POST', 'ack/..'], ['PUT', 'ack/all'],
  ['GET', '../../../api/plugins/other/x'], ['GET', 'healthz'], ['GET', 'card/t_1.json\n'], ['GET', null], [null, 'board.json']
]) eq(R(m, p), null, `${m} ${p} must be refused`)

// --- navigation targets
eq(mod.navTarget('board'), { page: 'board', id: null, all: false })
eq(mod.navTarget('board?all=1'), { page: 'board', id: null, all: true })
eq(mod.navTarget('card/t_9'), { page: 'card', id: 't_9', all: false })
for (const p of ['card/../x', 'card/', 'card/a/b', 'http://x', '/card/t_1', 'card/t_1?x=1', 5, null]) eq(mod.navTarget(p), null, String(p))

// --- the message handler: sender check, allowlist, reply target
const posted = []
const frameWin = { postMessage: (m, origin) => posted.push([m, origin]) }
const foreign = { postMessage: (m, origin) => posted.push(['FOREIGN', m, origin]) }
const views = []
const send = (source, data) => mod.handleFrameMessage(ctx, frameWin, { source, data }, v => views.push(v))

ctx.answer = p => (p.startsWith('/embed/ack') ? { ok: true, status: 200, text: 'cleared t_1\n' } : { cards: 3 })
await send(frameWin, { crew: 1, id: 1, method: 'GET', path: 'board.json?all=1' })
eq(rests.pop(), ['/board.json?all=1', 'GET'])
eq(posted.pop(), [{ crew: 1, id: 1, status: 200, text: '{"cards":3}' }, '*'])

await send(frameWin, { crew: 1, id: 2, method: 'POST', path: 'ack/t_1' })
eq(rests.pop(), ['/embed/ack/t_1', 'POST'])
eq(posted.pop(), [{ crew: 1, id: 2, status: 200, text: 'cleared t_1\n' }, '*'])

ctx.answer = () => ({ data_url: 'data:image/svg+xml;base64,AAAA' })
await send(frameWin, { crew: 1, id: 3, method: 'GET', path: 'avatars/role/worker.svg' })
eq(posted.pop(), [{ crew: 1, id: 3, status: 200, text: 'data:image/svg+xml;base64,AAAA' }, '*'])

const before = rests.length
await send(frameWin, { crew: 1, id: 4, method: 'GET', path: '../../api/plugins/other/x' })
eq(posted.pop(), [{ crew: 1, id: 4, error: 'crew: path not allowed' }, '*'])
await send(foreign, { crew: 1, id: 5, method: 'GET', path: 'board.json' })      // a different window
await send(null, { crew: 1, id: 6, method: 'GET', path: 'board.json' })         // no source
await send(frameWin, { id: 7, method: 'GET', path: 'board.json' })              // not ours (no crew marker)
await send(frameWin, { crew: 1, id: 'x', method: 'GET', path: 'board.json' })   // id must be a number
eq([rests.length, posted.length], [before, 0], 'foreign or malformed messages must do nothing')

await send(frameWin, { crew: 1, navigate: 'card/t_77' })
await send(frameWin, { crew: 1, navigate: 'card/../x' })
await send(foreign, { crew: 1, navigate: 'board' })
eq(views, [{ page: 'card', id: 't_77', all: false }])

// --- open chat: the target parser and the handler (the preload API is the only way to open a session)
const OT = mod.openSessionTarget
eq(OT({ profile: 'p1', session: 's_1' }), { profile: 'p1', session: 's_1' })
for (const v of [{ profile: 'p1', session: 'no-session' }, { profile: 'bad profile', session: 's' }, { profile: 'p', session: 'a/b' },
  { profile: 'p', session: '' }, { profile: 5, session: 's' }, 'p1/s_1', null, undefined]) eq(OT(v), null, JSON.stringify(v))
const opened = []
globalThis.window = { hermesDesktop: { openSessionWindow: (id, o) => opened.push([id, o]) } }
await send(frameWin, { crew: 1, openSession: { profile: 'p1', session: 's_1' } })
eq(opened, [['s_1', { profile: 'p1' }]])
await send(foreign, { crew: 1, openSession: { profile: 'p1', session: 's_2' } })
await send(frameWin, { crew: 1, openSession: { profile: 'p1', session: 'no-session' } })
await send(frameWin, { crew: 1, openSession: { profile: 'p1', session: '../x' } })
eq(opened.length, 1, 'foreign source or bad session opens nothing')
delete globalThis.window

ctx.answer = () => { throw new Error('boom') }
await send(frameWin, { crew: 1, id: 8, method: 'GET', path: 'board.json' })
eq(posted.pop(), [{ crew: 1, id: 8, error: 'boom' }, '*'])

// --- the page: loads /embed/board, hands SandboxedFrame a data: URL of the html, wires the message listener
const html = '<html><head></head><body>Crew ünï</body></html>'
ctx.answer = p => ({ html, nonce: 'n' })
globalThis.window = {}
globalThis.__hooks = { n: 0, state: {}, sets: [], effects: [] }
let tree = page.render()          // CrewPage element, not yet rendered: call the component
const render = () => { globalThis.__hooks.n = 0; globalThis.__hooks.effects = []; return tree.type(tree.props) }
let out = render()
eq(out.props.children[0], null, 'nothing is shown before the page arrives')
rests.length = 0
globalThis.__hooks.effects[0]()   // load effect
await new Promise(r => setTimeout(r, 5))
eq(rests.at(-1), ['/embed/board', undefined])
out = render()
const frame = out.props.children[0]
eq(frame.type, 'SandboxedFrame')
ok(frame.props.src.startsWith('data:text/html;charset=utf-8;base64,'), 'frame src is a data: URL')
eq(Buffer.from(frame.props.src.split(',')[1], 'base64').toString('utf8'), html, 'data URL round-trips the page, UTF-8 intact')
ok(!/allow-same-origin|sandbox/.test(JSON.stringify(frame.props)), 'no sandbox override from the plugin')
eq(out.props.children.length, 1, 'nothing above the frame: the page carries its own board link')
globalThis.__hooks.effects[1]()
eq([listeners.at(-1).target === globalThis.window, listeners.at(-1).type], [true, 'message'])
// a card view loads /embed/card/<id>, still with nothing above the frame
globalThis.__hooks.state[0] = { page: 'card', id: 't_77', all: false }
out = render()
eq(out.props.children[0].type, 'SandboxedFrame')
globalThis.__hooks.effects[0]()
await new Promise(r => setTimeout(r, 5))
eq(rests.at(-1)[0], '/embed/card/t_77')
console.log(`OK ${checks} checks`)
