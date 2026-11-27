import assert from 'node:assert/strict'
import fs from 'node:fs'
import { randomUUID } from 'node:crypto'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import vm from 'node:vm'
import { test } from 'node:test'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../host/mailbox-client.cjs',import.meta.url),'utf8')
test('the volume queue discards a malformed id and acknowledges a later valid request', t=>{
  const root=fs.mkdtempSync(join(tmpdir(),'dsh-queue-'))
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}))
  const inbox=join(root,'inbox')
  const map = path => path==='/app/data/dsh-source-updater' ? inbox :
    typeof path==='string' && path.startsWith('./') ? join(inbox,path.slice(2)) : path
  const scoped = new Proxy(fs,{get(target,key){
    if(typeof target[key]!=='function') return target[key]
    return (...args)=>target[key](...args.map(map))
  }})
  let line
  let reply
  vm.runInNewContext(source,{require:name=>name==='node:fs' ? scoped : name==='node:readline'
    ? {createInterface(){return {on(_event,handler){line=handler}}}} : require(name), Buffer,
    process:{getuid:()=>1000,chdir(){},stdin:null,stdout:{write(value){reply=JSON.parse(value)}}}})
  const rpc=body=>{line(JSON.stringify(body));return reply.result}
  const path=join(inbox,'request.json')
  const id=randomUUID()
  fs.writeFileSync(path,JSON.stringify({id:[id],action:'check'}),{mode:0o600})
  const [discard,invalidIdentity]=rpc({op:'request'})
  assert.equal(discard,null)
  rpc({op:'ack',identity:invalidIdentity})
  assert.equal(fs.existsSync(path),false)
  fs.writeFileSync(path,JSON.stringify({id,action:'check'}),{mode:0o600})
  const [valid,identity]=rpc({op:'request'})
  assert.deepEqual(valid,{id,action:'check'})
  rpc({op:'ack',identity})
  assert.equal(fs.existsSync(path),false)
})
