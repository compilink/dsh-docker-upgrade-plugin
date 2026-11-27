import assert from 'node:assert/strict'
import test from 'node:test'
import { captureInventory } from '../portable-inventory.js'

function fixture() {
  const ids = ['private-session-title', 'private-subagent']
  const histories = new Map(ids.map((id, index) => [id,
    Array.from({length:index ? 0 : 175},(_,seq) => ({type:'event',event:{
      seq, type:'user/message', time:1000+seq, data:{content:'private body ' + seq,x:[seq,null,true]},
    }})),
  ]))
  const requests = []
  const controller = {
    async list() {
      return {items:[{sessionId:ids[0]}, {sessionId:ids[1],parentSessionId:ids[0],origin:'subagent'}]}
    },
    async events(request) {
      requests.push(request)
      const {address,beforeSeq,throughSeq} = request
      const rows = histories.get(address.sessionId ?? address.childSessionId)
      if (address.kind === 'subagent') assert.equal(address.parentSessionId, ids[0])
      const cursor = throughSeq ?? rows.length - 1
      const end = Math.min(cursor + 1,beforeSeq)
      const start = Math.max(0,end - 24)
      return {cursor,records:rows.slice(start,end),hasMore:start>0}
    },
    modelCatalog() { throw Error('Must not contact model providers') },
  }
  const services = {
    sessionController:controller,
    llm:{listProviders:() => [{id:'private-provider'}]},
    agentDefaultModel:{currentSelection:() => ({provider:'private-provider',model:'private-model',reasoningEffort:'high'})},
  }
  return {ctx:{get:name=>services[name]}, ids,histories,requests,controller}
}

test('reads every page of a session and handles an empty durable subagent', async () => {
  const {ctx,requests} = fixture()
  const report = await captureInventory(ctx)
  assert.equal(report.schema, 1)
  assert.deepEqual(report.sessions.map(s=>s.events).sort((a,b)=>a-b), [0,175])
  assert.equal(report.providers.length,1)
  assert.ok(requests.filter(req=>req.beforeSeq>0).length>6)
  assert.doesNotMatch(JSON.stringify(report), /private|body|user\/message|reasoningEffort/)
})

test('pinned history admits appends but detects edited data at the same sequence', async () => {
  const {ctx,histories,ids} = fixture()
  const old = await captureInventory(ctx)
  const cuts = Object.fromEntries(old.sessions.map(s=>[s.id,s.cursor]))
  const data = histories.get(ids[0])
  data.push({type:'event',event:{seq:175,type:'turn/start',time:9901,data:{turn:9}}})
  assert.notDeepEqual(await captureInventory(ctx),old)
  assert.deepEqual(await captureInventory(ctx,{cuts}),old)
  const event = data[1].event
  event.data = {x:event.data.x,content:event.data.content}
  assert.deepEqual(await captureInventory(ctx,{cuts}),old)
  event.data.content = 'edited'
  assert.notDeepEqual(await captureInventory(ctx,{cuts}),old)
})

test('incomplete history fails without exposing the underlying error or identifiers', async () => {
  const {ctx,controller,histories,ids} = fixture()
  histories.get(ids[0])[8].event.seq = 77
  await assert.rejects(captureInventory(ctx), {message:'不能完整读取 DSH 会话或模型配置'})
  controller.events = async () => { throw Error('private body in provider error') }
  await assert.rejects(captureInventory(ctx), {message:'不能完整读取 DSH 会话或模型配置'})
})
