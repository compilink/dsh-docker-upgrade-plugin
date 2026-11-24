import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { apply } from '../index.js'

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'dsh-upgrade-web-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const dir = join(root, 'dsh-source-updater')
  const profile = join(root, 'profiles', 'web')
  mkdirSync(profile, { recursive: true })
  const routes = new Map()
  const owner = {}
  const ctx = {
    webServer: { protocol: 'http:', register(route) { routes.set(route.path, route.handler); return () => {} } },
    connection: { browserPeer: owner, admit(req) { return req.headers.cookie === 'test-owner' ? { peer: owner } : { rejection: 401 } } },
    agents: { list: () => [] }, profileContext: { dir: profile },
    emit(event) { assert.equal(event, 'apply-updates/check-idle') }, effect: fn => fn(),
  }
  apply(ctx)
  function request(route = 'status', method = 'GET', fields = {}, suffix = '') {
    const path = '/local/dsh-docker-upgrade-' + route
    const result = {}
    const headers = { host: 'localhost:3080', cookie: 'test-owner', ...fields }
    routes.get(path)({ method, url: path + suffix, headers }, {
      writeHead(status, headers) { result.status = status; result.headers = headers },
      end(value) { result.body = JSON.parse(value) },
    })
    return result
  }
  function save(name, value) { writeFileSync(join(dir, name), JSON.stringify(value)) }
  function online() {
    mkdirSync(dir, { recursive: true })
    save('worker.json', { schema: 1, scheduled: true, updatedAt: new Date().toISOString(), scheduleTime: '04:00', nextCheckAt: '2030-07-12T04:00:00+08:00' })
  }
  function auth() { return { origin: 'http://localhost:3080', 'x-dsh-upgrade-revision': request().body.revision } }
  return { dir, profile, ctx, request, save, online, auth }
}

test('indicates host availability and schedule without provisioning a queue', t => {
  const f = fixture(t)
  assert.equal(f.request().body.workerOnline, false)
  assert.equal(f.request('check', 'POST', f.auth()).status, 503)
  assert.equal(existsSync(f.dir), false)
  f.online()
  const { body } = f.request()
  assert.equal(body.workerOnline, true)
  assert.equal(body.scheduleTime, '04:00')
  assert.equal(body.nextCheckAt, '2030-07-11T20:00:00.000Z')
  f.save('worker.json', { updatedAt: new Date(Date.now() - 125000).toISOString() })
  assert.equal(f.request().body.workerOnline, false)
})

test('admits only the local owner, rejects cross-origin requests and foreign arguments', t => {
  const f = fixture(t)
  f.online()
  const auth = f.auth()
  assert.equal(f.request('status', 'GET', { cookie: '' }).status, 401)
  for (const hdr of [
    { host: 'elsewhere.example:3080' }, { origin: 'https://localhost:3080' },
    { forwarded: 'for=127.0.0.1' }, { 'x-forwarded-prefix': '/dsh' },
  ]) assert.equal(f.request('status', 'GET', hdr).status, 403)
  assert.equal(f.request('run', 'POST').status, 403)
  assert.equal(f.request('run', 'POST', { ...auth, cookie: '' }).status, 401)
  assert.equal(f.request('run', 'POST', { ...auth, origin: 'null' }).status, 403)
  assert.equal(f.request('run', 'GET', auth).status, 405)
  assert.equal(f.request('run', 'POST', auth, '?branch=master').status, 404)
  assert.equal(f.request('run', 'POST', { ...auth, 'content-length': '2' }).status, 400)
  assert.equal(f.request('run', 'POST', { ...auth, 'transfer-encoding': 'chunked' }).status, 400)
  assert.equal(f.request('run', 'POST', { origin: auth.origin }).status, 409)
  f.ctx.connection.admit = () => ({ peer: {} })
  assert.equal(f.request('status').status, 403)
  assert.equal(existsSync(join(f.dir, 'request.json')), false)
})

test('queues exactly one fixed action, keeps a durable result and rejects stale submissions', t => {
  const f = fixture(t)
  f.online()
  const auth = f.auth()
  const accepted = f.request('run', 'POST', auth)
  assert.equal(accepted.status, 202)
  const request = JSON.parse(readFileSync(join(f.dir, 'request.json'), 'utf8'))
  assert.deepEqual(request, { id: accepted.body.id, action: 'upgrade' })
  assert.equal(f.request('check', 'POST', auth).status, 409)
  assert.deepEqual(readdirSync(f.dir).sort(), ['request.json', 'worker.json'])
  assert.equal(f.request().body.pending, 'upgrade')
  const result = { schema: 1, action: 'upgrade', requestId: request.id, source: 'manual', phase: 'rolled-back', current: 'abcd001', latest: 'deadbee', message: '验证失败，已恢复', available: true, updatedAt: new Date().toISOString() }
  f.save('status.json', { ...result, privateLog: '/secret.log' })
  assert.equal(f.request().body.busy, true)
  unlinkSync(join(f.dir, 'request.json'))
  assert.equal(f.request('run', 'POST', auth).status, 409)
  apply(f.ctx)
  assert.equal(f.request().body.busy, false)
  assert.deepEqual(f.request().body.status, Object.fromEntries(Object.entries(result).filter(([key]) => key !== 'schema')))
  const afterRestart = f.request('check', 'POST', f.auth())
  assert.equal(afterRestart.status, 202)
  assert.equal(afterRestart.body.action, 'check')
})

test('blocks contention with channel delivery, queued work and package writes but allows a version check', t => {
  const f = fixture(t)
  f.online()
  const auth = f.auth()
  f.ctx.agents.list = () => [{ status: 'idle', inbox: { nextTurn: [], nextStep: [1] } }]
  assert.equal(f.request('run', 'POST', auth).status, 409)
  f.ctx.agents.list = () => []
  writeFileSync(join(f.profile, 'package.json.lock'), '')
  assert.equal(f.request('run', 'POST', auth).status, 409)
  unlinkSync(join(f.profile, 'package.json.lock'))
  f.ctx.emit = (_, check) => check.block('QQ queue active')
  assert.equal(f.request('run', 'POST', auth).body.error, 'QQ queue active')
  f.ctx.emit = () => { throw new Error('no signal') }
  assert.equal(f.request('run', 'POST', auth).status, 409)
  assert.equal(f.request('check', 'POST', auth).status, 202)
})

test('preserves a running scheduled job across a stale heartbeat, and a recovery error before any job', t => {
  const f = fixture(t)
  f.online()
  const job = { schema: 1, requestId: randomUUID(), action: 'upgrade', phase: 'validating', source: 'scheduled', updatedAt: new Date().toISOString() }
  f.save('status.json', job)
  f.save('worker.json', { updatedAt: '2000-01-01T00:00:00Z' })
  assert.deepEqual([f.request().body.busy, f.request().body.workerOnline], [true, false])
  assert.equal(f.request('run', 'POST', f.auth()).status, 409)
  f.online()
  f.save('status.json', { ...job, phase: 'blocked', requestId: null, action: null, message: '恢复失败' })
  assert.equal(f.request().body.status.message, '恢复失败')
  assert.equal(f.request('run', 'POST', f.auth()).status, 503)
})

test('rejects malformed and linked queue state without reading paths from requests', t => {
  const f = fixture(t)
  f.online()
  const filename = join(f.dir, 'status.json')
  f.save('status.json', { schema: 2 })
  assert.equal(f.request().status, 503)
  writeFileSync(filename, ' '.repeat(9000))
  assert.equal(f.request().status, 503)
  unlinkSync(filename)
  symlinkSync(join(f.dir, 'worker.json'), filename)
  assert.equal(f.request().status, 503)
  unlinkSync(filename)
  f.save('request.json', { id: randomUUID(), action: 'shell' })
  assert.equal(f.request().status, 503)
})

test('new DSH token scopes distinguish the browser from API and in-process owners', t => {
  const f = fixture(t)
  const browser = { principal: {kind:'owner'} }
  const api = { principal: {kind:'owner'} }
  f.ctx.get = name => name === 'dshLocalBrowserOwner' ? {matches: peer => peer === browser} : undefined
  for (const [peer,expected] of [[browser,200],[api,403],[f.ctx.connection.browserPeer,403]]) {
    f.ctx.connection.admit = () => ({peer})
    assert.equal(f.request().status,expected)
  }
  f.ctx.connection.admit = () => ({rejection:401})
  assert.equal(f.request().status,401)
})

test('mounts management routes under the authentication service on a scoped host', t => {
  const f = fixture(t)
  const registrations = []
  const dependencies = []
  const parent = {
    ...f.ctx,
    get: name => name === 'connection.authentication' ? {} : undefined,
    inject: (list, applyScoped) => {
      dependencies.push(...list)
      return applyScoped({ ...f.ctx, webServer:{register:route=>registrations.push(route.path)} })
    },
  }
  apply(parent)
  assert.deepEqual(dependencies,['connection.authentication'])
  assert.equal(registrations.length,3)
})
