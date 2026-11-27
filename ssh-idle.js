import { readFileSync, readdirSync, readlinkSync } from 'node:fs'
import { join } from 'node:path'

/** Check for attached terminals, SSH sessions and pending connections on sshd's listening ports. */
export function sshIdle(proc = '/proc', requiredRootInspection = false) {
  try {
    const listeners = new Set()
    let servers = 0
    for (const pid of readdirSync(proc).filter(name => /^\d+$/.test(name))) {
      const directory = join(proc, pid)
      try {
        const record = readFileSync(join(directory, 'stat'), 'utf8')
        const match = /^\d+ \((.*)\) (\S+) \d+ \d+ \d+ (-?\d+) /.exec(record)
        if (!match) return false
        if (['Z', 'X'].includes(match[2])) continue
        if (match[3] !== '0') return false
        if (['sshd-auth', 'sshd-session', 'sftp-server'].includes(match[1])) return false
        if (match[1] !== 'sshd') continue
        if (++servers > 1) return false
        let descriptors
        try { descriptors = readdirSync(join(directory, 'fd')) }
        catch (error) {
          if (!requiredRootInspection && ['EACCES', 'EPERM'].includes(error.code)) continue
          throw error
        }
        for (const fd of descriptors) {
          try {
            const socket = /^socket:\[(\d+)\]$/.exec(readlinkSync(join(directory, 'fd', fd)))
            if (socket) listeners.add(socket[1])
          } catch (error) {
            if (!requiredRootInspection && ['EACCES', 'EPERM'].includes(error.code)) continue
            if (error.code !== 'ENOENT') throw error
          }
        }
      } catch (error) { if (error.code !== 'ENOENT') throw error }
    }
    const sockets = []
    for (const table of ['tcp', 'tcp6']) {
      let contents
      try { contents = readFileSync(join(proc, 'net', table), 'utf8') }
      catch (error) { if (table === 'tcp6' && error.code === 'ENOENT') continue; throw error }
      const lines = contents.trim().split('\n')
      if (!lines[0].includes('local_address') || !lines[0].includes('inode')) return false
      for (const line of lines.slice(1)) {
        const fields = line.trim().split(/\s+/)
        if (fields.length < 10 || !/^[A-Fa-f0-9]+:[A-Fa-f0-9]{4}$/.test(fields[1])) return false
        sockets.push({ port: fields[1].slice(-4).toUpperCase(), state: fields[3], inode: fields[9] })
      }
    }
    const ports = new Set(['0016'])
    for (const item of sockets) if (item.state === '0A' && listeners.has(item.inode)) ports.add(item.port)
    return !sockets.some(item => ports.has(item.port) && !['0A', '06', '07'].includes(item.state))
  } catch { return false }
}
