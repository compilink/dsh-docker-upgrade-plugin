import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import vm from 'node:vm'

test('published web module registers its panel under the same id as the server bundle', () => {
  const source = readFileSync(new URL('../client.js', import.meta.url), 'utf8')
  const manifest = JSON.parse(readFileSync(new URL('../package.json', import.meta.url)))
  let registration
  let slot
  vm.runInNewContext(source, { window: { __ModuleLoader__: { load: value => { registration = value } } } })
  assert.equal(registration.id, manifest.name)
  assert.deepEqual(manifest.dsh.client.inject, ['@deepseek-ai/dsh-client-ui-settings'])
  assert.equal(manifest.dsh.client.platform, 'web')
  const plugin = registration.factory(name => { assert.equal(name, 'react'); return {} })
  plugin.apply({
    effect: run => run(),
    slots: { inject: (name, run) => { assert.equal(name, 'settings.section'); return run() }, register: (meta, panel) => { slot = { meta, panel } } },
  })
  assert.equal(slot.meta.name, 'settings.section')
  assert.equal(slot.meta.label(), 'DSH Docker 升级')
  assert.equal(typeof slot.panel, 'function')
  const patch = readFileSync(new URL('../cordis.patch.yml', import.meta.url), 'utf8')
  assert.ok(patch.includes('id: ' + slot.meta.id))
  assert.ok(patch.includes('name: ' + manifest.name))
})

test('offers the pinned Mac installer only after first-time status and updates the panel as the host connects', async t => {
  const source = readFileSync(new URL('../client.js', import.meta.url), 'utf8')
  const states = []
  const refs = []
  let stateIndex, refIndex, effect, panel, nextPoll, reply
  const react = {
    createElement: (type, props, ...children) => [{ type, props }, ...children.flat()],
    useState(initial) {
      const i = stateIndex++
      if (!(i in states)) states[i] = initial
      return [states[i], value => { states[i] = value }]
    },
    useRef(initial) { return refs[refIndex++] ??= { current: initial } },
    useEffect(run) { effect ??= run },
  }
  vm.runInNewContext(source, {
    window: { __ModuleLoader__: { load(entry) {
      entry.factory(() => react).apply({
        effect: fn => fn(),
        slots: { inject: (_, fn) => fn(), register: (_, render) => { panel = render } },
      })
    } } },
    document: { baseURI: 'http://localhost:3080/' }, URL, AbortController, AbortSignal,
    fetch: async url => {
      assert.equal(url.pathname, '/local/dsh-docker-upgrade-status')
      return { ok: reply.code === 200, status: reply.code, json: async () => reply.data }
    },
    setTimeout: fn => { nextPoll = fn; return 1 }, clearTimeout() {},
  })
  function render() {
    stateIndex = refIndex = 0
    const elements = panel()
    return {
      links: elements.filter(element => element?.type === 'a').map(element => element.props.href),
      disabled: elements.filter(element => element?.type === 'button').map(element => element.props.disabled),
      text: elements.filter(element => typeof element === 'string').join(' '),
    }
  }
  assert.equal(render().links.length, 0)
  reply = { code: 200, data: { workerState: 'missing', workerOnline: false, busy: false } }
  t.after(effect())
  await new Promise(setImmediate)
  const missing = render()
  assert.deepEqual(missing.links, ['https://github.com/compilink/dsh-docker-upgrade-plugin/releases/download/v0.2.0/DSH-Docker-Updater-macOS.zip'])
  assert.match(missing.text, /运行 Docker Desktop 的 Mac/)
  for (const [workerState, message] of [['stale', /新的心跳/], ['blocked', /暂停处理/], ['online', /已连接/]]) {
    reply.data = { workerState, workerOnline: workerState === 'online', busy: false }
    await nextPoll()
    const current = render()
    assert.equal(current.links.length, 0)
    assert.deepEqual(current.disabled, [workerState !== 'online', workerState !== 'online'])
    assert.match(current.text, message)
  }
  reply = { code: 403, data: { error: '请用本机所有者账号登录' } }
  await nextPoll()
  const denied = render()
  assert.equal(denied.links.length, 0)
  assert.match(denied.text, /所有者账号登录/)
  assert.doesNotMatch(denied.text, /已连接/)
  assert.deepEqual(denied.disabled, [true, true])
})
