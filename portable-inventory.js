import { createHash } from 'node:crypto'
import { setImmediate as yieldToHost } from 'node:timers/promises'

const digest = (domain, value) => createHash('sha256').update(domain + '\0').update(JSON.stringify(value)).digest('hex')
const validCut = value => Number.isSafeInteger(value) && value >= -1 && value < Number.MAX_SAFE_INTEGER

function requireValue(ok) {
  if (!ok) throw Error('不能完整读取 DSH 会话或模型配置')
}

// Object insertion order is not part of a stored JSON event.
function writeJson(hash, value) {
  if (Array.isArray(value)) {
    hash.update('[')
    for (const item of value) {
      writeJson(hash, item)
      hash.update(',')
    }
    hash.update(']')
  } else if (value !== null && typeof value === 'object') {
    hash.update('{')
    for (const name of Object.keys(value).sort()) {
      hash.update(JSON.stringify(name) + ':')
      writeJson(hash, value[name])
      hash.update(',')
    }
    hash.update('}')
  } else {
    hash.update(JSON.stringify(value))
  }
}

async function inventoryRow(controller, row, cuts, signal) {
  requireValue(typeof row.sessionId === 'string' && row.sessionId.length > 0)
  let address = {kind: 'session', sessionId: row.sessionId}
  if (row.origin === 'subagent') {
    requireValue(typeof row.parentSessionId === 'string' && row.parentSessionId.length > 0)
    address = {kind: 'subagent', parentSessionId: row.parentSessionId, childSessionId: row.sessionId, mode: 'unknown'}
  }
  const id = digest('session', [row.sessionId])
  const head = await controller.events({address, beforeSeq: 0, maxMessages: 1}, signal)
  requireValue(validCut(head.cursor) && Array.isArray(head.records) && head.records.length === 0)
  const cursor = cuts !== undefined && Object.hasOwn(cuts, id) ? cuts[id] : head.cursor
  requireValue(validCut(cursor) && cursor <= head.cursor)
  const hash = createHash('sha256').update('DSH history v1\0')
  let next = cursor + 1
  while (next > 0) {
    signal.throwIfAborted()
    const page = await controller.events({address, throughSeq: cursor, beforeSeq: next, maxMessages: 64}, signal)
    requireValue(page.cursor === cursor && Array.isArray(page.records) && page.records.length > 0)
    // Backward pagination fixes the read cut even if another caller appends new events.
    for (let i = page.records.length - 1; i >= 0; --i) {
      const record = page.records[i]
      requireValue(record?.type === 'event' && record.event?.seq === next - 1)
      writeJson(hash, record.event)
      hash.update('\n')
      if (--next % 128 === 0) await yieldToHost()
    }
    requireValue(page.hasMore === (next > 0))
  }
  return {id, cursor, events: cursor + 1, history: hash.digest('hex')}
}

/**
 * Read the registered routes, configured default and the contiguous event history of every visible session.
 * Optional cuts pin selected hashed sessions to the cursor previously observed by the host.
 * No provider catalog refresh, credential service, model call or agent activation is needed.
 */
export async function captureInventory(ctx, {cuts, signal = AbortSignal.timeout(120_000)} = {}) {
  try {
    const controller = ctx.get('sessionController')
    const defaults = ctx.get('agentDefaultModel').currentSelection()
    requireValue(typeof defaults.provider === 'string' && defaults.provider && typeof defaults.model === 'string' && defaults.model)
    const defaultModel = digest('default', [defaults.provider, defaults.model, defaults.reasoningEffort ?? null])
    const providers = ctx.get('llm').listProviders().map(provider => {
      requireValue(typeof provider.id === 'string' && provider.id)
      return digest('provider', [provider.id])
    }).sort()
    requireValue(new Set(providers).size === providers.length)
    const {items} = await controller.list({}, signal)
    requireValue(Array.isArray(items))
    const sessions = []
    for (const item of items) {
      signal.throwIfAborted()
      sessions.push(await inventoryRow(controller, item, cuts, signal))
    }
    sessions.sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
    requireValue(new Set(sessions.map(row => row.id)).size === sessions.length)
    return {schema: 1, defaultModel, providers, sessions}
  } catch {
    // A controller error may contain session identifiers, workspace paths or event values.
    throw Error('不能完整读取 DSH 会话或模型配置')
  }
}
