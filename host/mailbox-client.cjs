// A restricted, non-networked helper receives only the selected inbox subdirectory.
const fs = require('node:fs')
const { randomUUID } = require('node:crypto')
const { createInterface } = require('node:readline')
const inbox = '/app/data/dsh-source-updater'
if (process.getuid() === 0) throw Error('refuse root mailbox')
try { fs.mkdirSync(inbox, { mode: 0o700 }) } catch (e) { if (e.code !== 'EEXIST') throw e }
const dir = fs.openSync(inbox, fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW)
try {
  const st = fs.fstatSync(dir)
  if (!st.isDirectory() || st.mode & 0o022) throw Error('unsafe mailbox permissions')
  // cwd pins the verified directory even if its visible entry is replaced while requests are polled.
  process.chdir('/proc/self/fd/' + dir)
} finally { fs.closeSync(dir) }
const root = '.'
const request = root + '/request.json'
const allowed = new Set(['status.json','worker.json'])
const validId = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
function inode(st) { return [String(st.dev), String(st.ino)] }
function perform({ op, name, value, identity }) {
  if (op === 'write' && allowed.has(name)) {
    const temp = root + '/.' + randomUUID()
    try {
      fs.writeFileSync(temp, JSON.stringify(value) + '\n', { flag: 'wx', mode: 0o644 })
      fs.renameSync(temp, root + '/' + name)
    } finally { try { fs.unlinkSync(temp) } catch (e) { if (e.code !== 'ENOENT') throw e } }
    return true
  }
  const stat = fs.lstatSync(request, { bigint: true, throwIfNoEntry: false })
  if (!stat) return null
  const current = inode(stat)
  if (op === 'ack') {
    if (Array.isArray(identity) && identity[0] === current[0] && identity[1] === current[1] && !stat.isDirectory()) fs.unlinkSync(request)
    return null
  }
  if (op !== 'request') throw Error()
  if (stat.isFile() && stat.nlink !== 1n) return null
  if (!stat.isFile() || stat.size > 1024n) return [null, current]
  const fd = fs.openSync(request, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK)
  try {
    const f = fs.fstatSync(fd, { bigint: true })
    if (!f.isFile() || f.size > 1024n || f.nlink !== 1n || f.ino !== stat.ino || f.dev !== stat.dev) return null
    const data = Buffer.alloc(1025)
    const size = fs.readSync(fd, data)
    if (size > 1024) return [null, current]
    try {
      const job = JSON.parse(data.toString('utf8',0,size))
      if (job && typeof job === 'object' && !Array.isArray(job) && Object.keys(job).length === 2 &&
          typeof job.id === 'string' && validId.test(job.id) && ['check','upgrade'].includes(job.action)) return [job,current]
    } catch {}
    return [null,current]
  } finally { fs.closeSync(fd) }
}
for (const channel of [createInterface({ input: process.stdin })]) channel.on('line', line => {
  try {
    if (line.length > 16384) throw Error()
    process.stdout.write(JSON.stringify({ result: perform(JSON.parse(line)) }) + '\n')
  } catch { process.stdout.write('{"error":true}\n') }
})
