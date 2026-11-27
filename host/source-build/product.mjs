/** Install the CLI's runtime closure and verify the source of every first-party package in it. */
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { createRequire } from 'node:module'
import { acceptsNpmPlatform } from '/src/scripts/npm-platform.ts'

const json = path => JSON.parse(readFileSync(path, 'utf8'))
const root = '/product'
const stash = '.dsh-source-packages'
const {load} = createRequire('/src/package.json')('js-yaml')
const locked = load(readFileSync('/src/pnpm-lock.yaml','utf8')).packages
const packs = new Map()
for (const group of ['dsh', 'vendor', 'native']) {
  for (const file of readdirSync('/packs/' + group).filter(name => name.endsWith('.tgz'))) {
    const path = join('/packs', group, file)
    const manifest = JSON.parse(execFileSync('tar', ['-xzOf', path, 'package/package.json'], {encoding:'utf8'}))
    if (packs.has(manifest.name)) throw Error('Duplicate upstream package')
    packs.set(manifest.name, {manifest, path})
  }
}

const required = new Set()
const supportsHost = manifest => acceptsNpmPlatform(manifest.os, process.platform) && acceptsNpmPlatform(manifest.cpu, process.arch)
function select(name) {
  if (required.has(name)) return
  const {manifest} = packs.get(name)
  required.add(name)
  if (!supportsHost(manifest)) return
  for (const [dep, spec] of Object.entries({...manifest.dependencies, ...manifest.optionalDependencies, ...manifest.peerDependencies})) {
    if (packs.has(dep)) select(dep)
    else if (dep.startsWith('@deepseek-ai/') && !Object.hasOwn(manifest.optionalDependencies ?? {}, dep)
          && (dep.startsWith('@deepseek-ai/dsh') || !locked[dep + '@' + spec]?.resolution?.integrity)) {
      throw Error('Missing first-party source package or locked external dependency: ' + dep)
    }
  }
}
select('@deepseek-ai/dsh')
mkdirSync(join(root, stash), {recursive:true})
const dependencies = {}
const optionalDependencies = {}
const integrity = new Map()
for (const name of [...required].sort()) {
  const {path, manifest} = packs.get(name)
  const filename = basename(path)
  cpSync(path, join(root, stash, filename))
  const section = supportsHost(manifest) ? dependencies : optionalDependencies
  section[name] = 'file:' + stash + '/' + filename
  integrity.set(name, 'sha512-' + createHash('sha512').update(readFileSync(path)).digest('base64'))
}
writeFileSync(join(root, 'package.json'), JSON.stringify({private:true, dependencies, optionalDependencies}, null, 2) + '\n')
execFileSync('npm', ['install', '--omit=dev', '--no-fund', '--no-audit', '--registry=https://registry.npmjs.org/'],
             {cwd:root, stdio:'inherit'})
const lock = json(join(root, 'package-lock.json')).packages
const installed = new Set()
for (const [directory, record] of Object.entries(lock)) {
  const name = directory.split('node_modules/').at(-1)
  if (!name.startsWith('@deepseek-ai/')) continue
  const file = join(root, directory, 'package.json')
  if (!existsSync(file)) continue // Foreign-platform optional dependencies are present only in the lock.
  if (!packs.has(name) && !name.startsWith('@deepseek-ai/dsh')
      && record.integrity && record.integrity === locked[name + '@' + record.version]?.resolution?.integrity) continue
  if (!integrity.has(name) || record.integrity !== integrity.get(name) || record.resolved !== dependencies[name]) {
    throw Error('Installed first-party dependency differs from the packed source: ' + name)
  }
  installed.add(name)
}
for (const name of Object.keys(dependencies)) if (!installed.has(name)) throw Error('Missing installed source package: ' + name)
const native = '@deepseek-ai/node-addon-system-linux-' + process.arch
if (!installed.has(native)) throw Error('Missing local Linux platform package')
const platform = join(root, 'node_modules', native)
const inventory = json(join(platform,'prebuilds.json')).binaries
const flock = inventory.find(binary => binary.tool === 'flock' && binary.libc === 'glibc')
if (!flock || !inventory.every(binary => existsSync(join(platform, binary.path)))) throw Error('Incomplete native binaries')
createRequire(import.meta.url)(join(platform, flock.path))
const cli = join(root, 'node_modules/@deepseek-ai/dsh')
if (execFileSync(process.execPath, [join(cli, 'lib/bin.js'), '--version'], {encoding:'utf8'}).trim() !== json(join(cli, 'package.json')).version) {
  throw Error('Installed DSH CLI version mismatch')
}
writeFileSync(join(root, '.dsh-source-revision'), process.env.DSH_CLIENT_COMMIT_HASH + '\n')
