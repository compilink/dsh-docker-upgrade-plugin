import { createHash, randomUUID } from 'node:crypto'
import { closeSync, constants, fstatSync, fsyncSync, linkSync, lstatSync, openSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { mountPortableControl } from './portable-control.js'
import { captureInventory } from './portable-inventory.js'
import { sshIdle } from './ssh-idle.js'

export const name = 'docker-upgrade'
export const inject = ['webServer', 'connection', 'agents', 'profileContext']

const endpoint = '/local/dsh-docker-upgrade'
const actions = new Set(['check', 'upgrade'])
const active = new Set(['checking', 'building', 'validating', 'backing-up', 'deploying', 'verifying'])
const phases = new Set([...active, 'idle', 'succeeded', 'failed', 'rolled-back', 'blocked'])
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const commit = /^[0-9a-f]{7,64}$/i
const heartbeatTtl = 120_000

function jsonFile(dir, name) {
  let fd
  try {
    fd = openSync(join(dir, name), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  } catch (err) {
    if (err.code === 'ENOENT') return null
    throw err
  }
  try {
    const stat = fstatSync(fd)
    if (!stat.isFile() || stat.size > 8192) throw new Error('invalid file')
    const value = JSON.parse(readFileSync(fd, 'utf8'))
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid data')
    return value
  } finally {
    closeSync(fd)
  }
}

function readStatus(dir) {
  const status = jsonFile(dir, 'status.json')
  if (!status) return null
  if (status.schema !== 1 || !phases.has(status.phase) || !Number.isFinite(Date.parse(status.updatedAt))) throw new Error('invalid status')
  const workerStatus = ['idle', 'blocked'].includes(status.phase) && status.requestId === null && status.action === null
  if (!workerStatus && (!uuid.test(status.requestId) || !actions.has(status.action))) throw new Error('invalid operation')
  return {
    requestId: status.requestId,
    action: status.action,
    source: ['scheduled', 'manual'].includes(status.source) ? status.source : null,
    phase: status.phase,
    updatedAt: new Date(status.updatedAt).toISOString(),
    current: commit.test(status.current) ? status.current : null,
    latest: commit.test(status.latest) ? status.latest : null,
    available: typeof status.available === 'boolean' ? status.available : null,
    message: typeof status.message === 'string' ? status.message.slice(0, 300) : '',
  }
}

function header(req, name) {
  return typeof req.headers.get === 'function' ? req.headers.get(name) ?? undefined : req.headers[name]
}

function localAuthority(req, protocol) {
  const host = header(req, 'host')
  if (typeof host !== 'string' || !/^(127\.0\.0\.1|localhost|\[::1\])(?::\d{1,5})?$/i.test(host)) return null
  const keys = typeof req.headers.keys === 'function' ? [...req.headers.keys()] : Object.keys(req.headers)
  if (keys.some(key => key.toLowerCase() === 'forwarded' || key.toLowerCase().startsWith('x-forwarded-'))) return null
  const authority = URL.parse(protocol + '//' + host)
  if (!authority) return null
  const origin = header(req, 'origin')
  return (req.method === 'GET' && origin === undefined) || origin === authority.origin ? authority : null
}

function idleBlocker(ctx) {
  if (ctx.agents.list().some(agent => agent.status !== 'idle' || agent.inbox.nextTurn.length || agent.inbox.nextStep.length)) return 'DSH 有运行中或排队的会话'
  for (const file of ['package.json.lock', '.plugin-manager/run.json']) {
    if (lstatSync(join(ctx.profileContext.dir, file), { throwIfNoEntry: false })) return '插件安装或更新尚未结束'
  }
  let blocked
  ctx.emit('apply-updates/check-idle', { block(reason) { blocked ??= String(reason).slice(0, 180) } })
  return blocked ?? null
}

function snapshot(ctx, dir, boot) {
  const folder = lstatSync(dir, { throwIfNoEntry: false })
  if (folder && !folder.isDirectory()) throw new Error('invalid queue')
  const worker = jsonFile(dir, 'worker.json')
  const age = Date.now() - Date.parse(worker?.updatedAt)
  const heartbeatFresh = age >= -5000 && age <= heartbeatTtl
  const scheduleTime = /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(worker?.scheduleTime) ? worker.scheduleTime : null
  const nextCheckAt = Number.isFinite(Date.parse(worker?.nextCheckAt)) ? new Date(worker.nextCheckAt).toISOString() : null
  const status = readStatus(dir)
  const request = jsonFile(dir, 'request.json')
  if (request && (!uuid.test(request.id) || !actions.has(request.action))) throw new Error('invalid request')
  let workerState = 'missing'
  if (heartbeatFresh) workerState = status?.phase === 'blocked' && status.requestId === null ? 'blocked' : 'online'
  else if (worker || status || request) workerState = 'stale'
  const workerOnline = workerState === 'online'
  const storage = worker?.storage
  const mounts = ['data', 'workspace'].every(k => ['bind', 'volume'].includes(storage?.[k]))
    ? { data: storage.data, workspace: storage.workspace } : null
  const busy = !!request || active.has(status?.phase)
  const pending = request && status?.requestId !== request.id ? request.action : null
  let blocker = null
  if (!busy && workerOnline) {
    try { blocker = idleBlocker(ctx) } catch { blocker = '暂时无法核实 DSH 是否空闲' }
  }
  return { revision: boot + '/' + (status?.requestId ?? 'initial'), workerState, workerOnline, busy, pending, blocker, status, scheduleTime, nextCheckAt, mounts }
}

// Publish complete JSON with no overwrite, even when multiple processes share the queue.
function enqueue(dir, action) {
  const id = randomUUID()
  const temp = join(dir, '.request-' + id)
  const fd = openSync(temp, 'wx', 0o600)
  try {
    try {
      writeFileSync(fd, JSON.stringify({ id, action }) + '\n')
      fsyncSync(fd)
    } finally { closeSync(fd) }
    linkSync(temp, join(dir, 'request.json'))
  } finally { unlinkSync(temp) }
  return id
}

function send(res, code, value) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' })
  res.end(JSON.stringify(value))
}

function isBrowserOwner(ctx, peer, req, authority) {
  if (!peer) return false
  const browser = ctx.get?.('dshLocalBrowserOwner')
  if (browser) return browser.matches(peer)
  const auth = ctx.get?.('connection.authentication')
  if (!auth) return peer === ctx.connection.browserPeer
  if (peer.principal?.kind !== 'owner') return false

  // Let the stock token provider verify the browser cookie without any API credentials.
  const audience = authority.protocol === 'https:' ? authority.origin : authority.host
  const name = 'dsh-auth-' + createHash('sha256').update(audience).digest('base64url')
  const cookies = String(header(req, 'cookie') ?? '').split(';').map(value => value.trim()).filter(value => value.startsWith(name + '='))
  if (cookies.length !== 1 || !cookies[0].startsWith(name + '=v1.')) return false
  return auth.authenticate({ headers: { host: header(req, 'host'), cookie: cookies[0] }, socket: req.socket, signal: req.signal }).peer === peer
}

/** Register the owner's local upgrade settings API. The host worker owns all deployment parameters. */
export function apply(ctx) {
  if (ctx.get?.('connection.authentication') !== undefined) {
    ctx.inject(['connection.authentication'], scoped => mount(scoped))
    return
  }
  return mount(ctx)
}

function mount(ctx) {
  const dir = resolve(ctx.profileContext.dir, '../..', 'dsh-source-updater')
  const boot = randomUUID()
  ctx.effect(() => mountPortableControl(boot, () => idleBlocker(ctx) || !sshIdle(), options => captureInventory(ctx, options)))
  const routes = [{ path: endpoint + '-status', method: 'GET' }, { path: endpoint + '-check', method: 'POST', action: 'check' }, { path: endpoint + '-run', method: 'POST', action: 'upgrade' }]
  for (const { path, method, action } of routes) {
    const handler = (req, res) => {
      const authority = localAuthority(req, ctx.webServer.protocol)
      if (!authority) return send(res, 403, { error: '请在运行 DSH 的电脑上，通过本机回环地址直接打开设置。' })
      const admission = ctx.connection.admit(req)
      if (admission.rejection || !isBrowserOwner(ctx, admission.peer, req, authority)) return send(res, admission.rejection || 403, { error: '请先用本机 DSH 所有者账号登录' })
      if (req.url !== path) return send(res, 404, { error: '路径无效' })
      if (req.method !== method) return send(res, 405, { error: '请求方法无效' })
      if (action && (header(req, 'transfer-encoding') !== undefined || ![undefined, '0'].includes(header(req, 'content-length')))) return send(res, 400, { error: '请求不接受参数' })
      try {
        const state = snapshot(ctx, dir, boot)
        if (!action) return send(res, 200, state)
        if (state.busy) return send(res, 409, { error: '已有检查或升级在执行' })
        if (!state.workerOnline) return send(res, 503, { error: '宿主机升级服务未就绪' })
        if (header(req, 'x-dsh-upgrade-revision') !== state.revision) return send(res, 409, { error: '升级状态已变化，请重新确认' })
        if (action === 'upgrade' && state.blocker) return send(res, 409, { error: state.blocker })
        const id = enqueue(dir, action)
        return send(res, 202, { id, action })
      } catch (err) {
        return err.code === 'EEXIST' ? send(res, 409, { error: '已有请求在等待处理' }) : send(res, 503, { error: '无法读写宿主机升级状态，请检查升级服务' })
      }
    }
    ctx.effect(() => ctx.webServer.register({ kind: 'exact', path, handler }))
  }
}
