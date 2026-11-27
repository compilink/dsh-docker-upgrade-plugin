// Executed only by the native worker through Docker exec in the selected container.
const fs = require('node:fs')
const crypto = require('node:crypto')
const net = require('node:net')
const { createInterface } = require('node:readline')
const dir = '/run/dsh-portable-update-v1'

function checkDirectory(path) {
  const st = fs.lstatSync(path)
  if (!st.isDirectory() || st.uid !== 0 || st.mode & 0o022) throw Error('unsafe control directory')
}
function nodeGroup() {
  const entry = /^node:[^:]*:\d+:(\d+):/m.exec(fs.readFileSync('/etc/passwd', 'utf8'))
  if (!entry || Number(entry[1]) <= 0) throw Error('service group unavailable')
  return Number(entry[1])
}
function bytes(name) {
  const fd = fs.openSync(dir + '/' + name, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK)
  try {
    const st = fs.fstatSync(fd)
    if (!st.isFile() || st.uid !== 0 || st.nlink !== 1 || st.size > 128 || st.mode & (name === 'private.der' ? 0o077 : 0o022)) throw Error('unsafe control file')
    if (name === 'reply.key' && ((st.mode & 0o777) !== 0o440 || st.gid !== nodeGroup() || st.size !== 32)) throw Error('unsafe reply key')
    return fs.readFileSync(fd)
  } finally { fs.closeSync(fd) }
}
function saveFile(path, value, mode, group) {
  const fd = fs.openSync(path, 'wx', 0o600)
  try {
    fs.writeFileSync(fd, value)
    if (group !== undefined) fs.fchownSync(fd, 0, group)
    fs.fchmodSync(fd, mode)
    fs.fsyncSync(fd)
  } finally { fs.closeSync(fd) }
}
function saveReply(path, privateBytes) {
  const reply = crypto.createHmac('sha256', privateBytes).update('dsh-portable-reply-v1').digest()
  saveFile(path, reply, 0o440, nodeGroup())
}
function verifyPublic(expected) {
  const fd = fs.openSync(dir + '/public.der', fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK)
  try {
    const st = fs.fstatSync(fd)
    if (!st.isFile() || st.uid !== 0 || st.nlink !== 1 || (st.mode & 0o022) || st.size !== expected.length ||
        !expected.equals(fs.readFileSync(fd))) throw Error('control key mismatch')
    // Older control installs created the verified public file using the container's umask.
    if ((st.mode & 0o777) !== 0o644) {
      fs.fchmodSync(fd, 0o644)
      fs.fsyncSync(fd)
    }
  } finally { fs.closeSync(fd) }
}
function syncDirectory(path) {
  const fd = fs.openSync(path, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW)
  try { fs.fsyncSync(fd) }
  finally { fs.closeSync(fd) }
}
function cleanAbandonedStages() {
  for (const name of fs.readdirSync('/run')) {
    const match = /^\.dsh-update-keys-([1-9][0-9]*)-[A-Za-z0-9]{6}$/.exec(name)
    if (!match) continue
    try { process.kill(Number(match[1]), 0); continue } catch (err) { if (err.code !== 'ESRCH') continue }
    const stage = '/run/' + name
    try {
      checkDirectory(stage)
      const entries = fs.readdirSync(stage)
      if (entries.some(file => !['private.der','public.der','reply.key'].includes(file))) continue
      for (const file of entries) fs.unlinkSync(stage + '/' + file)
      fs.rmdirSync(stage)
    } catch (err) { if (err.code !== 'ENOENT') throw err }
  }
}
function provision() {
  cleanAbandonedStages()
  try { checkDirectory(dir) } catch (e) { if (e.code !== 'ENOENT') throw e }
  let key
  try { key = readKey() } catch (e) {
    if (e.code !== 'ENOENT') throw e
    // Rename a complete pair into place. An interrupted writer cannot expose a partial final DER.
    const stage = fs.mkdtempSync('/run/.dsh-update-keys-' + process.pid + '-')
    try {
      const pair = crypto.generateKeyPairSync('ed25519')
      const secret = pair.privateKey.export({ type: 'pkcs8', format: 'der' })
      saveFile(stage + '/private.der', secret, 0o600)
      saveFile(stage + '/public.der', pair.publicKey.export({ type: 'spki', format: 'der' }), 0o644)
      saveReply(stage + '/reply.key', secret)
      fs.chmodSync(stage, 0o755)
      syncDirectory(stage)
      try { fs.renameSync(stage, dir) } catch (err) {
        if (!['EEXIST', 'ENOTEMPTY'].includes(err.code)) throw err
      }
      syncDirectory('/run')
    } finally {
      // The unique staging directory belongs to this invocation alone.
      for (const file of ['private.der','public.der','reply.key']) {
        try { fs.unlinkSync(stage + '/' + file) } catch (err) { if (err.code !== 'ENOENT') throw err }
      }
      try { fs.rmdirSync(stage) } catch (err) { if (err.code !== 'ENOENT') throw err }
    }
    checkDirectory(dir)
    key = readKey()
  }
  const pub = crypto.createPublicKey(key).export({ type: 'spki', format: 'der' })
  try { bytes('reply.key') } catch (e) {
    if (e.code !== 'ENOENT') throw e
    const stage = dir + '/.reply-' + crypto.randomUUID()
    try {
      saveReply(stage, bytes('private.der'))
      fs.renameSync(stage, dir + '/reply.key')
      syncDirectory(dir)
    } finally { try { fs.unlinkSync(stage) } catch (err) { if (err.code !== 'ENOENT') throw err } }
  }
  try { verifyPublic(pub) } catch (e) {
    if (e.code !== 'ENOENT') throw e
    const stage = dir + '/.public-' + crypto.randomUUID()
    try {
      saveFile(stage, pub, 0o644)
      fs.renameSync(stage, dir + '/public.der')
      syncDirectory(dir)
    } finally { try { fs.unlinkSync(stage) } catch (err) { if (err.code !== 'ENOENT') throw err } }
  }
}
function readKey() { return crypto.createPrivateKey({ key: bytes('private.der'), format: 'der', type: 'pkcs8' }) }
async function run(op, expectedBoot) {
  if (process.getuid() !== 0 || !['activate','status','stop','inventory'].includes(op)) throw Error('invalid request')
  checkDirectory('/run')
  if (op === 'activate') { provision(); return { active: true } }
  checkDirectory(dir)
  const key = readKey()
  const replyKey = bytes('reply.key')
  function verified(message, kind) {
    const { mac, ...value } = message
    if (typeof mac !== 'string' || !/^[0-9a-f]{64}$/.test(mac)) throw Error('invalid peer')
    const wanted = crypto.createHmac('sha256', replyKey).update(JSON.stringify([kind,value])).digest()
    if (!crypto.timingSafeEqual(wanted, Buffer.from(mac,'hex'))) throw Error('invalid peer')
    return value
  }
  const address = '\0dsh-portable-' + crypto.createHash('sha256').update(bytes('public.der')).digest('hex')
  const socket = net.createConnection(address)
  const timeout = setTimeout(() => socket.destroy(Error('timeout')), op === 'inventory' ? 120000 : 6000)
  const lines = createInterface({ input: socket, crlfDelay: Infinity })
  try {
    const stream = lines[Symbol.asyncIterator]()
    const hello = verified(JSON.parse((await stream.next()).value), 'hello')
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
    if (!uuid.test(hello.boot) || !uuid.test(hello.challenge) || (op === 'stop' && expectedBoot !== hello.boot)) throw Error('instance changed')
    let sshBusy = false
    if (op !== 'inventory') {
      const source = fs.readFileSync(0)
      if (!source.length || source.length > 32768) throw Error('missing native SSH check')
      const { sshIdle } = await import('data:text/javascript;base64,' + source.toString('base64'))
      sshBusy = !sshIdle('/proc', true)
    }
    if (sshBusy && op === 'stop') return { boot: hello.boot, busy: true, accepted: false }
    const proof = [1, hello.boot, hello.challenge, op]
    const request = { op }
    if (op === 'inventory') {
      const payload = fs.readFileSync(0)
      if (payload.length > 4 * 1024 * 1024 - 1024) throw Error('inventory request too large')
      request.cuts = JSON.parse(payload.toString('utf8'))
      proof.push(request.cuts)
    }
    request.signature = crypto.sign(null, Buffer.from(JSON.stringify(proof)), key).toString('base64')
    socket.write(JSON.stringify(request) + '\n')
    const ack = verified(JSON.parse((await stream.next()).value), op)
    if (ack.boot !== hello.boot || ack.challenge !== hello.challenge) throw Error('invalid reply')
    if (op === 'inventory' && ack.inventory && typeof ack.inventory === 'object') return ack.inventory
    if (typeof ack.busy !== 'boolean' || typeof ack.accepted !== 'boolean') throw Error('invalid reply')
    const result = { boot: ack.boot, busy: ack.busy || sshBusy, accepted: ack.accepted }
    if (op === 'stop' && result.accepted) {
      await new Promise((resolve, reject) => process.stdout.write(JSON.stringify(result) + '\n', err => err ? reject(err) : resolve()))
      await new Promise((resolve, reject) => socket.end('ok\n', err => err ? reject(err) : resolve()))
      return
    }
    return result
  } finally { clearTimeout(timeout); lines.close(); socket.destroy() }
}
run(process.argv[2],process.argv[3]).then(result => {
  if (result !== undefined) process.stdout.write(JSON.stringify(result) + '\n')
}).catch(() => { process.exitCode = 1 })
