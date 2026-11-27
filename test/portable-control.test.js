import assert from 'node:assert/strict'
import { createHmac, generateKeyPairSync, randomBytes, randomUUID, sign } from 'node:crypto'
import { mkdtempSync, rmSync } from 'node:fs'
import { createConnection } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { once } from 'node:events'
import { createInterface } from 'node:readline'
import { test } from 'node:test'
import { controlServer } from '../portable-control.js'

test('a root-provisioned key can stop only its current idle DSH process once', async t => {
  const pair = generateKeyPairSync('ed25519')
  const other = generateKeyPairSync('ed25519')
  const boot = randomUUID()
  const replyKey = randomBytes(32)
  let busy = true
  let stops = 0
  const root = mkdtempSync(join(tmpdir(),'dsh-ctrl-'))
  const path = join(root,'ctrl.sock')
  const control = controlServer({ key: pair.publicKey, replyKey, boot, busy: () => busy, stop: () => { stops++ },
    inventory: ({ cuts }) => ({ sessionsChecked: Object.keys(cuts ?? {}).length }) })
  t.after(() => { control.close(); rmSync(root,{ recursive:true,force:true }) })
  control.server.listen(path)
  await once(control.server,'listening')
  function verified(line, kind) {
    const { mac, ...value } = JSON.parse(line)
    assert.equal(mac, createHmac('sha256', replyKey).update(JSON.stringify([kind, value])).digest('hex'))
    return value
  }
  async function send(op, key, previous, cuts, sentCuts = cuts) {
    const socket = createConnection(path)
    const lines = createInterface({ input:socket })[Symbol.asyncIterator]()
    try {
      const hello = verified((await lines.next()).value, 'hello')
      assert.equal(hello.boot,boot)
      const proof = [1, boot, previous ?? hello.challenge, op]
      if (op === 'inventory') proof.push(cuts ?? null)
      const signature = sign(null, Buffer.from(JSON.stringify(proof)), key).toString('base64')
      socket.write(JSON.stringify({ op, signature, cuts: sentCuts }) + '\n')
      const reply = await lines.next()
      if (reply.done) return null
      const value = verified(reply.value, op)
      if (op === 'stop' && value.accepted) {
        socket.end('ok\n')
        await lines.next()
      }
      return value
    } finally { socket.destroy() }
  }
  assert.equal((await send('status',pair.privateKey)).busy,true)
  const cuts = { ['f'.repeat(64)]: 2 }
  assert.deepEqual((await send('inventory', pair.privateKey, undefined, cuts)).inventory, { sessionsChecked: 1 })
  assert.equal(await send('inventory', pair.privateKey, undefined, cuts, {}), null)
  assert.equal(await send('inventory', pair.privateKey, undefined, { invalid: -1 }), null)
  assert.equal((await send('stop',pair.privateKey)).accepted,false)
  busy = false
  assert.equal(await send('stop',other.privateKey),null)
  assert.equal(await send('stop',pair.privateKey,randomUUID()),null)
  assert.equal(stops,0)
  assert.equal((await send('stop',pair.privateKey)).accepted,true)
  assert.equal((await send('stop',pair.privateKey)).accepted,false)
  assert.equal(stops,1)
})
