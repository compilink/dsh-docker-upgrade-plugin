import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { test } from 'node:test'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../host/control-client.cjs', import.meta.url), 'utf8')
const nodeGid = 2847
const virtualDirectory = '/run/dsh-portable-update-v1'

function machine(t) {
  const root = fs.mkdtempSync(join(tmpdir(), 'dsh-private-keys-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const directory = join(root, 'dsh-portable-update-v1')
  const groups = new Map()
  const inode = stat => stat.dev + '/' + stat.ino
  const actual = path => typeof path === 'string' && (path === '/run' || path.startsWith('/run/')) ? root + path.slice(4) : path
  const io = { interruptAt: null }
  // Exercise real files, permissions, renames and crypto. Only uid/gid and the in-container
  // passwd entry are simulated so the test can run as an ordinary macOS user.
  const fixed = new Proxy(fs, { get(target, method) {
    if (typeof target[method] !== 'function') return target[method]
    return (...args) => {
      if (method === 'readFileSync' && args[0] === '/etc/passwd') {
        return 'node:x:1926:' + nodeGid + ':fixture:/home/node:/bin/false\n'
      }
      if (method === 'chownSync' || method === 'fchownSync') {
        assert.equal(args[1], 0)
        assert.equal(args[2], nodeGid)
        const st = method === 'chownSync' ? fs.statSync(actual(args[0])) : fs.fstatSync(args[0])
        groups.set(inode(st), args[2])
        return
      }
      if (method === 'renameSync' && io.interruptAt === args[1]) throw Error('interrupted publication')
      const mapped = args.map(actual)
      if (method === 'openSync' && mapped[1] === 'wx' && typeof mapped[2] === 'number') mapped[2] &= 0o700
      const result = target[method](...mapped)
      if (method === 'lstatSync' || method === 'fstatSync') {
        result.uid = 0
        result.gid = groups.get(inode(result)) ?? 0
      }
      return result
    }
  } })
  async function activate() {
    const agent = { pid: process.pid, argv: ['node', 'control', 'activate'], stdout: { write() {} }, getuid: () => 0,
      kill() { throw Object.assign(Error('no stale process'), { code: 'ESRCH' }) } }
    await vm.runInNewContext(source, { require: name => name === 'node:fs' ? fixed : require(name),
      process: agent, Buffer, setTimeout, clearTimeout })
    return agent.exitCode ?? 0
  }
  return { root, directory, io, activate, group(path) { return groups.get(inode(fs.statSync(path))) },
    setGroup(path, gid) { groups.set(inode(fs.statSync(path)), gid) } }
}

test('an interrupted initial publication exposes no keys and a retry publishes one complete key set', async t => {
  const m = machine(t)
  m.io.interruptAt = virtualDirectory
  assert.equal(await m.activate(), 1)
  assert.equal(fs.existsSync(m.directory), false)
  const stale = join(m.root, '.dsh-update-keys-90000-abcdef')
  fs.mkdirSync(stale, { mode: 0o700 })
  for (const name of ['private.der', 'public.der', 'reply.key']) fs.writeFileSync(join(stale, name), 'interrupted', { mode: 0o600 })
  m.io.interruptAt = null
  assert.equal(await m.activate(), 0)
  assert.equal(fs.existsSync(stale), false)
  assert.deepEqual(fs.readdirSync(m.directory).sort(), ['private.der','public.der','reply.key'])
  const ke = join(m.directory, 'private.der'), pub = join(m.directory, 'public.der'), reply = join(m.directory, 'reply.key')
  const secret = fs.readFileSync(ke)
  assert.equal(fs.statSync(ke).mode & 0o777, 0o600)
  assert.equal(fs.statSync(pub).mode & 0o777, 0o644)
  assert.deepEqual(crypto.createPublicKey(crypto.createPrivateKey({ key: secret, format: 'der', type: 'pkcs8' }))
    .export({ format: 'der', type: 'spki' }), fs.readFileSync(pub))
  assert.equal(fs.statSync(reply).mode & 0o777, 0o440)
  assert.equal(m.group(reply), nodeGid)
  assert.equal(fs.statSync(reply).size, 32)
  assert.deepEqual(fs.readFileSync(reply), crypto.createHmac('sha256', secret).update('dsh-portable-reply-v1').digest())
})

test('missing reply and public keys recover from the same private key, including after interrupted repair', async t => {
  const m = machine(t)
  assert.equal(await m.activate(), 0)
  const key = join(m.directory,'private.der'), pub = join(m.directory,'public.der'), reply = join(m.directory,'reply.key')
  const saved = [key, pub, reply].map(f => fs.readFileSync(f))
  fs.unlinkSync(pub)
  fs.unlinkSync(reply)
  m.io.interruptAt = virtualDirectory + '/reply.key'
  assert.equal(await m.activate(), 1)
  assert.equal(fs.existsSync(reply), false)
  assert.deepEqual(fs.readFileSync(key), saved[0])
  m.io.interruptAt = null
  assert.equal(await m.activate(), 0)
  assert.deepEqual([key, pub, reply].map(f => fs.readFileSync(f)), saved)
  assert.equal(m.group(reply), nodeGid)
  assert.equal(fs.statSync(reply).mode & 0o777, 0o440)
})

test('an existing reply key with a different group, mode or size is rejected', async t => {
  const m = machine(t)
  assert.equal(await m.activate(), 0)
  const reply = join(m.directory, 'reply.key')
  m.setGroup(reply, nodeGid + 1)
  assert.equal(await m.activate(), 1)
  m.setGroup(reply, nodeGid)
  fs.chmodSync(reply, 0o444)
  assert.equal(await m.activate(), 1)
  fs.chmodSync(reply, 0o600)
  fs.writeFileSync(reply, Buffer.alloc(31))
  fs.chmodSync(reply, 0o440)
  assert.equal(await m.activate(), 1)
})

test('a restrictive legacy public-key mode is fixed only after matching the installed root key', async t => {
  const m = machine(t)
  assert.equal(await m.activate(), 0)
  const pub = join(m.directory, 'public.der')
  const original = fs.readFileSync(pub)
  fs.chmodSync(pub, 0o600)
  assert.equal(await m.activate(), 0)
  assert.equal(fs.statSync(pub).mode & 0o777, 0o644)
  assert.deepEqual(fs.readFileSync(pub), original)
  fs.chmodSync(pub, 0o600)
  fs.writeFileSync(pub, crypto.randomBytes(original.length))
  assert.equal(await m.activate(), 1)
  assert.equal(fs.statSync(pub).mode & 0o777, 0o600)
})
