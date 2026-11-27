import { createHash, createHmac, createPublicKey, randomUUID, verify } from 'node:crypto'
import { closeSync, constants, fstatSync, lstatSync, openSync, readFileSync } from 'node:fs'
import { createServer } from 'node:net'

const directory = '/run/dsh-portable-update-v1'
const uuid = /^[a-f0-9-]{36}$/

function activation() {
  for (const path of ['/run', directory]) {
    const stat = lstatSync(path, { throwIfNoEntry: false })
    if (!stat?.isDirectory() || stat.uid !== 0 || (stat.mode & 0o022)) return null
  }
  let fd
  try {
    fd = openSync(directory + '/public.der', constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    const stat = fstatSync(fd)
    if (!stat.isFile() || stat.nlink !== 1 || stat.uid !== 0 || (stat.mode & 0o022) || stat.size !== 44) return null
    const der = readFileSync(fd)
    const key = createPublicKey({ key: der, format: 'der', type: 'spki' })
    if (key.asymmetricKeyType !== 'ed25519') return null
    closeSync(fd)
    fd = undefined
    fd = openSync(directory + '/reply.key', constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    const replyStat = fstatSync(fd)
    if (!replyStat.isFile() || replyStat.nlink !== 1 || replyStat.uid !== 0 ||
        (replyStat.mode & 0o777) !== 0o440 || replyStat.size !== 32) return null
    return { key, replyKey: readFileSync(fd), address: '\0dsh-portable-' + createHash('sha256').update(der).digest('hex') }
  } catch { return null } finally { if (fd !== undefined) closeSync(fd) }
}

// The native installer provisions the public key outside the DSH data mount.
export function controlServer({ key, replyKey, boot, busy, stop, inventory }) {
  let stopping = false
  const sockets = new Set()
  function authenticated(kind, value) {
    const mac = createHmac('sha256', replyKey).update(JSON.stringify([kind,value])).digest('hex')
    return JSON.stringify({ ...value, mac }) + '\n'
  }
  const server = createServer(socket => {
    sockets.add(socket)
    socket.on('close', () => sockets.delete(socket))
    socket.on('error', () => socket.destroy())
    const deadline = setTimeout(() => socket.destroy(), 5000)
    deadline.unref()
    socket.on('close', () => clearTimeout(deadline))
    const challenge = randomUUID()
    socket.write(authenticated('hello', { challenge, boot }))
    let bytes = ''
    socket.setEncoding('utf8')
    async function read(chunk) {
      bytes += chunk
      if (bytes.length > 4 * 1024 * 1024) return socket.destroy()
      if (!bytes.includes('\n')) return
      socket.removeListener('data', read)
      try {
        const { op, signature, cuts } = JSON.parse(bytes)
        if (!['status', 'stop', 'inventory'].includes(op) || typeof signature !== 'string' || signature.length !== 88) return socket.destroy()
        const proof = [1, boot, challenge, op]
        if (op === 'inventory') proof.push(cuts ?? null)
        if (!verify(null, Buffer.from(JSON.stringify(proof)), key, Buffer.from(signature, 'base64'))) return socket.destroy()
        if (op === 'inventory') {
          if (cuts != null && (typeof cuts !== 'object' || Array.isArray(cuts) || Object.entries(cuts).some(
            ([id, cursor]) => !/^[0-9a-f]{64}$/.test(id) || !Number.isSafeInteger(cursor) || cursor < -1))) return socket.destroy()
          clearTimeout(deadline)
          socket.setTimeout(120000, () => socket.destroy())
          const value = await inventory({ cuts: cuts ?? undefined })
          const reply = authenticated(op, { boot, challenge, inventory: value })
          if (reply.length > 8 * 1024 * 1024) return socket.destroy()
          return socket.end(reply)
        }
        const blocked = stopping || !!busy()
        const accepted = op === 'stop' && !blocked
        const ack = authenticated(op, { boot, challenge, busy: blocked, accepted })
        if (!accepted) return socket.end(ack)
        stopping = true
        let receipt = ''
        let committed = false
        socket.on('close', () => { if (!committed) stopping = false })
        // The root client returns the signed acknowledgement to the host before SIGTERM can end Docker exec.
        socket.write(ack)
        socket.on('data', part => {
          receipt += part
          if (receipt.length > 3) return socket.destroy()
          if (receipt === 'ok\n') {
            try {
              if (!busy()) {
                committed = true
                socket.end()
                stop()
              } else socket.end()
            } catch { socket.destroy() }
          }
        })
      } catch { socket.destroy() }
    }
    socket.on('data', read)
  })
  return { server, close() { for (const socket of sockets) socket.destroy(); server.close() } }
}

export function mountPortableControl(boot, busy, inventory) {
  if (process.platform !== 'linux' || !uuid.test(boot)) return () => {}
  let active
  function findHost() {
    if (active) return
    const ready = activation()
    if (!ready) return
    const control = controlServer({ key: ready.key, replyKey: ready.replyKey, boot, inventory,
      busy: () => !process.listenerCount('SIGTERM') || busy(), stop: () => process.kill(process.pid, 'SIGTERM') })
    active = control
    control.server.on('error', () => { control.close(); if (active === control) active = undefined })
    control.server.listen(ready.address)
    control.server.unref()
  }
  findHost()
  const timer = setInterval(findHost, 2000)
  timer.unref()
  return () => { clearInterval(timer); active?.close() }
}
