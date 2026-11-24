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
