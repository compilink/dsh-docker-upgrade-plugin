/** Apply the verified npm product to the base image offline; preserve unrelated runtime files. */
import { cpSync, existsSync, lstatSync, readFileSync, realpathSync, rmSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'

const app = '/app'
const cli = '/app/apps/cli'
const canonical = '/app/node_modules/@deepseek-ai/dsh'
if (realpathSync(cli) !== canonical || !lstatSync(cli).isSymbolicLink() || realpathSync(app) !== app) {
  throw Error('The original image does not use the supported CLI installation layout')
}
const manifest = JSON.parse(readFileSync('/app/package.json', 'utf8'))
if (Object.keys(manifest).join() !== 'dependencies' ||
    Object.keys(manifest.dependencies ?? {}).join() !== '@deepseek-ai/dsh') {
  throw Error('The original /app has additional npm inputs; automatic replacement is unavailable')
}
if (existsSync('/app/.dsh-source-packages') || existsSync('/app/.dsh-source-revision')) throw Error('Unexpected source payload in baseline')
const owner = statSync('/app/node_modules')
for (const name of ['node_modules','package.json','package-lock.json','.dsh-source-packages','.dsh-source-revision']) {
  const target = join(app, name)
  if (existsSync(target)) {
    if (lstatSync(target).isSymbolicLink()) throw Error('Unexpected link in npm root')
    rmSync(target, {recursive:true})
  }
  cpSync(join('/dsh-build-product',name),target,{recursive:true,verbatimSymlinks:true,preserveTimestamps:true})
  if (process.getuid() === 0) execFileSync('chown',['-Rh',owner.uid + ':' + owner.gid, target])
}
const built = JSON.parse(readFileSync(canonical + '/package.json', 'utf8'))
if (execFileSync(process.execPath, [cli + '/lib/bin.js','--version'],{encoding:'utf8'}).trim() !== built.version) {
  throw Error('The original CLI path does not launch the new product')
}
