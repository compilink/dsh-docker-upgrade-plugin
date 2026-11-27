"""Validate a source image on copies, then replace only the selected Compose service."""
import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import socket
import tarfile
import tempfile
import time
import uuid
from urllib.parse import urlencode

import onboard
from portable_control import docker_cli
from portable_inventory import assert_preserved, cuts


class TransactionError(RuntimeError):
    """A deployment failure safe to show in the settings panel."""


IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
ID = re.compile(r'[0-9a-f]{64}')
SHA = re.compile(r'[0-9a-f]{40}')
LABEL = 'io.github.compilink.dsh.upgrade-transaction'
STABLE = ('context', 'endpoint', 'project', 'service', 'container', 'composeFile', 'projectDirectory',
          'hostPort', 'containerPort', 'imageRef', 'storage', 'launchConfigSHA256', 'composeConfigSHA256')
JOURNAL = 'portable-transaction.json'
RECEIPT = 'portable-validation.json'
CACHE_LEAF = re.compile(r'/tmp/node-addon-native-custom-loader-([0-9]+)/native-cache/'
                        r'node-addon-require-builtin-linux-(arm64|x64)-gnu/[0-9]+\.[0-9]+\.[0-9]+/'
                        r'linux-\2-gnu/linux-\2-gnu-napi-v[0-9]+\.node')
CACHE_HASH = """
const fs=require('node:fs'), crypto=require('node:crypto');
if(process.argv[2]==='rebuild') {try {require('/app/node_modules/node-addon-require-builtin')} catch {}}
const fd=fs.openSync(process.argv[1],fs.constants.O_NOFOLLOW|fs.constants.O_RDONLY|fs.constants.O_NONBLOCK);
const st=fs.fstatSync(fd);if(!st.isFile()||st.nlink!==1||st.size>67108864) process.exit(1);
console.log(crypto.createHash('sha256').update(fs.readFileSync(fd)).digest('hex'));
"""
RUNTIME_PATH = """
const fs=require('node:fs'), path=process.argv[1],st=fs.lstatSync(path);
if(path==='/run/sshd.pid') {
 if(!st.isFile()||st.nlink!==1||st.size>32) process.exit(1);
 const pid=fs.readFileSync(path,'utf8').trim();
 if(!/^[1-9][0-9]*$/.test(pid)||fs.readFileSync('/proc/'+pid+'/comm','utf8').trim()!=='sshd') process.exit(1);
} else if(path.startsWith('/tmp/dsh-')) {
 if(!st.isDirectory()||fs.readdirSync(path).length) process.exit(1);
} else if(path==='/run/dsh-portable-update-v1') {
 if(!st.isDirectory()||st.uid!==0||(st.mode&0o022)) process.exit(1);
} else if(!st.isFile()||st.nlink!==1||st.uid!==0||st.size>128||(st.mode&(path.endsWith('private.der')?0o077:0o022))) process.exit(1);
"""
SSH_KEYS = r'''
set -euo pipefail
keys=$(ssh-keyscan -T 4 -p 22 -t ed25519,rsa,ecdsa 127.0.0.1 2>/dev/null | awk 'NF==3 {print $2,$3}' | sort -u)
[[ -n "$keys" ]]
printf '%s\n' "$keys" | sha256sum | cut -d' ' -f1
'''


def require(ok, message):
    if not ok:
        raise TransactionError(message)


def identity(binding):
    return onboard.digest({name: binding[name] for name in STABLE})


def save(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(value, ensure_ascii=True).encode() + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read(path):
    if not path.exists():
        return None
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 16 * 1024 * 1024, '宿主事务记录无效。')
    return json.loads(path.read_bytes())


def mount_arg(mount, target, *, readonly=False):
    source = mount['source']
    require(mount['type'] in ('bind', 'volume') and isinstance(source, str) and source
            and not any(c in source for c in (',', '"', '\r', '\n', '\x00')), '不能安全绑定此存储路径。')
    if mount['type'] == 'volume':
        require(onboard.NAME.fullmatch(source), 'Docker 卷名无效。')
    else:
        require(Path(source).is_absolute() and Path(source).resolve(strict=True) == Path(source), '宿主存储路径已变化。')
    return f"type={mount['type']},src={source},dst={target}" + (',readonly' if readonly else '')


def equivalent_ref(left, right):
    def normalized(value):
        return value if ':' in value.rsplit('/', 1)[-1] or '@' in value else value + ':latest'
    return normalized(left) == normalized(right)


def same_storage(left, right):
    if left['type'] != right['type']:
        return False
    if left['type'] == 'volume':
        return left['source'] == right['source']
    a, b = Path(left['source']), Path(right['source'])
    return a == b or a in b.parents or b in a.parents


def allowed_changes(changes, mount_points):
    """Only return runtime leaves with a separate regeneration or identity check."""
    leaves, parents, modified = {}, set(), set()
    for change in changes:
        path, kind = change['Path'], change['Kind']
        require(isinstance(path, str) and kind in (0, 1) and path.startswith('/')
                and all(c >= ' ' for c in path) and str(Path(path)) == path,
                '容器可写层包含未备份的修改。')
        if path in mount_points:
            pass
        elif re.fullmatch(r'/tmp/dsh-(?:spill|subprocess)-[a-zA-Z0-9]{6}', path) or path == '/run/sshd.pid':
            leaves[path] = 'runtime'
        elif path in ('/run/dsh-portable-update-v1', '/run/dsh-portable-update-v1/public.der',
                      '/run/dsh-portable-update-v1/private.der', '/run/dsh-portable-update-v1/reply.key'):
            leaves[path] = 'runtime'
        elif CACHE_LEAF.fullmatch(path):
            leaves[path] = 'cache'
        else:
            modified.add(path)
            continue
        parents.update(str(parent) for parent in Path(path).parents)
    require(modified <= parents, '容器可写层有持久挂载之外的修改。请先迁移手装文件、SSH 配置或其他工作数据。')
    return leaves


# GNU tar sorts directory children, records hard links, xattrs and ACLs, and
# never follows symlinks. Skip only the worker's continuously updated mailbox.
VOLUME_TASK = r'''
set -euo pipefail
mode=$1; kind=$2
options=(--sort=name --format=posix --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime --numeric-owner --xattrs '--xattrs-include=*' --acls)
list() {
  if [[ $kind == data ]]; then
    find . -mindepth 1 -maxdepth 1 ! -name dsh-source-updater -print0
  else
    find . -mindepth 1 -maxdepth 1 -print0
  fi
}
archive() (
  cd "$1"
  special=(-mindepth 1 -xdev)
  if [[ $kind == data ]]; then special+=(-path ./dsh-source-updater -prune -o); fi
  unusual=$(find . "${special[@]}" '!' -type d '!' -type f '!' -type l -print -quit) || exit 1
  [[ -z "$unusual" ]] || exit 1
  list | sort -z | tar "${options[@]}" --null -T - -cf -
)
rootmeta() (cd "$1" && tar "${options[@]}" "${@:2}" --no-recursion -cf - .)
digest() { { rootmeta "$1" --mtime=@0 && archive "$1"; } | sha256sum | cut -d' ' -f1; }
before=$(digest /source)
if [[ $mode == fingerprint ]]; then echo "$before"; exit 0; fi
if [[ $mode == restore || $mode == restore-bind ]]; then
  [[ $before == "$3" ]]
  (cd /target && list | xargs -0r rm -rf --)
else
  [[ -z "$(find /target -mindepth 1 -maxdepth 1 -print -quit)" ]]
fi
extract=(--numeric-owner --same-owner --same-permissions --xattrs '--xattrs-include=*' --acls -xf -)
archive /source | (cd /target && tar "${extract[@]}")
rootmeta /source | (cd /target && tar "${extract[@]}")
if [[ $mode != restore-bind ]]; then [[ $(digest /target) == "$before" ]]; fi
[[ $(digest /source) == "$before" ]]
echo "$before"
'''
DIRECTORY_TIMES = """
const fs=require('node:fs');
const kind=process.argv[1],root=Buffer.from('/source'),slash=Buffer.from('/'),rows=[];
function walk(path,subpath) {
  rows.push([subpath.toString('base64'),String(fs.lstatSync(path,{bigint:true}).mtimeNs)]);
  for(const name of fs.readdirSync(path,{encoding:'buffer'})) {
    if(!subpath.length && kind==='data' && name.equals(Buffer.from('dsh-source-updater'))) continue;
    const child=Buffer.concat([path,slash,name]);
    if(fs.lstatSync(child).isDirectory()) walk(child, subpath.length?Buffer.concat([subpath,slash,name]):name)
  }
}
walk(root,Buffer.alloc(0));console.log(JSON.stringify(rows));
"""
WEB_CHECK = r"""
import { request } from 'node:http';
import { networkInterfaces } from 'node:os';
const port=Number(process.argv[1]);
const addresses=['127.0.0.1', ...Object.values(networkInterfaces()).flat().filter(i=>i?.family==='IPv4'&&!i.internal).map(i=>i.address)];
function read(host,path) { return new Promise((resolve,reject)=>{
  const req=request({host,port,path,headers:{host:'127.0.0.1:'+port}, signal:AbortSignal.timeout(2500)},res=>{
    let text='';res.setEncoding('utf8').on('data',part=>{if(text.length<65536) text+=part.slice(0,65536-text.length)})
      .on('end',()=>resolve({status:res.statusCode,ct:res.headers['content-type']||'',text}));
  }); req.on('error',reject);req.end();
}); }
for(const address of addresses) { try {
  const main=await read(address,'/');
  const gate=await read(address,'/local/dsh-docker-upgrade-status');
  const loginPage = main.status===200 && /<html/i.test(main.text);
  const loginRequired = main.status===401 && (/text\/plain|application\/json/i.test(main.ct) || /<html/i.test(main.text));
  // Newer hosts reject before dispatching private routes. The signed control handshake verifies the mounted plugin.
  if((loginPage || loginRequired) && [401,403].includes(gate.status)) process.exit(0);
} catch {} }
process.exit(1);
"""


class Docker:
    def __init__(self, binding, state):
        self.b = binding
        self.state = state
        require(onboard.local_context() == (binding['context'], binding['endpoint']) and binding['docker'] == onboard.DOCKER,
                '本机 Docker 连接目标已变化。')
        require(set(binding['storage']) == {'data', 'workspace'}
                and binding['storage']['data']['target'] == '/app/data' and binding['storage']['workspace']['target'] == '/workspace',
                '需要明确绑定 DSH 数据和工作区。')
        for row in binding['storage'].values():
            mount_arg(row, row['target'])
            if row['type'] == 'bind':
                path = Path(row['source'])
                require(path != state and path not in state.parents and state not in path.parents, '宿主事务目录必须独立于容器挂载。')
        require(not same_storage(*binding['storage'].values()), '数据目录与工作区必须独立。')
        ref = binding['imageRef']
        require(isinstance(ref, str) and re.fullmatch(r'[a-z0-9][a-z0-9._/-]*(?::[\w.-]+)?', ref)
                and '@' not in ref and not ref.startswith('sha256:'), '需要可固定到本机镜像的 Compose 引用。')

    def cli(self, *args, timeout=45):
        return onboard.run([*docker_cli(self.b), *args], timeout=timeout)

    def compose(self, *args):
        return self.cli('compose', '--project-directory', self.b['projectDirectory'], '--project-name', self.b['project'],
                        '--file', self.b['composeFile'], *args, timeout=180)

    def current(self):
        value = self.compose('ps', '--all', '--quiet', self.b['service']).strip()
        require(not value or ID.fullmatch(value), '目标 Compose 服务必须仅有一个容器。')
        return value

    def info(self, cid):
        require(ID.fullmatch(cid), 'Docker 容器标识无效。')
        return json.loads(self.cli('inspect', '--type', 'container', '--format', onboard.INSPECT, cid))

    def inspect(self, cid, template):
        return json.loads(self.cli('inspect', '--type', 'container', '--format', template, cid))

    def config_hash(self, cid):
        digest = self.info(cid)['labels'].get('com.docker.compose.config-hash')
        require(isinstance(digest, str) and ID.fullmatch(digest), '当前 Compose 服务缺少配置指纹。')
        return digest

    def image(self, ref):
        image = self.cli('image', 'inspect', '--format', '{{.Id}}', ref)
        require(IMAGE.fullmatch(image), '本机镜像标识无效。')
        return image

    def changes(self, cid):
        require(ID.fullmatch(cid), '容器标识无效。')
        # Docker's /changes preserves newline-bearing filenames which the text diff cannot distinguish.
        _, raw = self.api('GET', '/containers/' + cid + '/changes', 4194304)
        changes = json.loads(raw)
        require(changes is None or isinstance(changes, list), '容器可写层报告无效。')
        return changes or []

    def api(self, method, path, limit):
        connection = http.client.HTTPConnection('localhost', timeout=25)
        try:
            connection.sock = socket.socket(socket.AF_UNIX)
            connection.sock.settimeout(25)
            connection.sock.connect(self.b['endpoint'].removeprefix('unix://'))
            connection.request(method, path)
            response = connection.getresponse()
            raw = response.read(limit + 1)
            require(response.status == 200 and len(raw) <= limit, '无法完整核对容器可写层。')
            return response.getheader('X-Docker-Container-Path-Stat'), raw
        finally:
            connection.close()

    def writable(self, cid, image, previous=None):
        leaves = allowed_changes(self.changes(cid), {m['target'] for m in self.b['storage'].values()})
        if previous is not None:
            require(leaves.items() <= previous.items(), '服务关闭时出现新的未核实文件。')
            return leaves
        running = self.info(cid)['running']
        for path, kind in leaves.items():
            if not running:
                self.stopped_leaf(cid, image, path, kind)
                continue
            if kind == 'runtime':
                self.cli('exec', '--user', '0', '--env', 'NODE_OPTIONS=', cid, '/usr/local/bin/node', '-e', RUNTIME_PATH, path)
            else:
                actual = self.cli('exec', '--user', '0', '--env', 'NODE_OPTIONS=', cid, '/usr/local/bin/node', '-e', CACHE_HASH, path)
                self.match_cache(image, path, actual)
        return leaves

    def match_cache(self, image, path, actual):
        uid = CACHE_LEAF.fullmatch(path)[1]
        expected = self.cli('run', '--rm', '--pull=never', '--network', 'none', '--read-only', '--user', uid,
                            '--cap-drop', 'ALL', '--tmpfs', '/tmp:rw,exec,size=64m', '--env', 'NODE_OPTIONS=',
                            '--entrypoint', '/usr/local/bin/node', image, '-e', CACHE_HASH, path, 'rebuild')
        require(ID.fullmatch(actual) and actual == expected, '容器内原生缓存与镜像重新生成的内容不同。')

    def stopped_leaf(self, cid, image, path, kind):
        # Docker can stat a stopped container. Never request the contents of a control key.
        route = '/containers/' + cid + '/archive?' + urlencode({'path':path})
        header, _ = self.api('HEAD', route, 0)
        info = json.loads(base64.b64decode(header, validate=True))
        mode, size = info['mode'], info['size']
        require(not info.get('linkTarget'), '容器运行文件的类型不符合预期。')
        if path.startswith('/tmp/dsh-') or path == '/run/dsh-portable-update-v1':
            require(mode >> 31 == 1, '容器临时目录的类型已改变。')
            return
        sizes = {'public.der':44, 'private.der':48, 'reply.key':32}
        if Path(path).name in sizes:
            require(mode >> 9 == 0 and size == sizes[Path(path).name], '容器控制文件的元数据已改变。')
            return
        require(mode >> 9 == 0 and (0 < size <= 64 * 1024 * 1024 if kind == 'cache' else 0 < size <= 32),
                '容器运行文件的大小或类型异常。')
        _, raw = self.api('GET', route, size + 65536)
        with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
            items = archive.getmembers()
            require(len(items) == 1 and items[0].isreg() and items[0].size == size, '无法读取容器运行文件。')
            contents = archive.extractfile(items[0]).read()
        if kind == 'cache':
            self.match_cache(image, path, hashlib.sha256(contents).hexdigest())
        else:
            require(re.fullmatch(rb'[1-9][0-9]*\n?', contents), 'SSH 服务的 PID 文件不符合预期。')

    def ssh_keys(self, cid):
        if not self.b.get('sshExposed') and not self.b.get('sshPublished'):
            return None
        digest = self.cli('exec', '--env', 'BASH_ENV=', cid, '/bin/bash', '-c', SSH_KEYS)
        require(ID.fullmatch(digest), '无法确认容器 SSH 服务端的公钥。')
        return digest

    def storage_ids(self, helper):
        result = {}
        for kind, mount in self.b['storage'].items():
            if mount['type'] == 'bind':
                st = Path(mount['source']).stat(follow_symlinks=False)
                result[kind] = onboard.digest(['bind', st.st_dev, st.st_ino])
            else:
                meta = self.cli('volume', 'inspect', '--format',
                                '[{{json .Name}},{{json .CreatedAt}},{{json .Driver}},{{json .Scope}},{{json .Options}}]', mount['source'])
                name, created, driver, scope, options = json.loads(meta)
                require(name == mount['source'] and created and driver == 'local' and scope == 'local' and not options,
                        '命名卷必须是能够核验物理身份的本机普通 Docker 卷。')
                inode = self.cli('run', '--rm', '--pull=never', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                                 '--entrypoint', '/usr/bin/stat', '--mount', mount_arg(mount, '/payload', readonly=True),
                                 helper, '-c', '%d:%i', '/payload')
                require(re.fullmatch(r'[0-9]+:[0-9]+', inode), '无法核验命名卷的物理身份。')
                result[kind] = onboard.digest([name, created, driver, scope, inode])
        return result

    def bound(self, cid, image, *, running=True):
        require(cid == self.current(), 'Compose 实例在操作期间已替换。')
        info = self.info(cid)
        self.mounts(info)
        labels = info['labels']
        require(info['id'] == cid and info['image'] == image and
                labels.get('com.docker.compose.project') == self.b['project'] and
                labels.get('com.docker.compose.service') == self.b['service'] and
                (not running or info['running']), '目标容器或镜像已变化。')
        require(onboard.compose_service(self.b, self.b['storage'], labels.get('com.docker.compose.config-hash')) == self.b['composeConfigSHA256']
                and onboard.digest(info['launch']) == self.b['launchConfigSHA256'], 'Compose 配置或容器启动方式已变化。')
        return info

    def mounts(self, info):
        rows = info.get('mounts') or []
        require(len(rows) == 2, '发现额外容器挂载，无法保证完整备份。')
        for mount in self.b['storage'].values():
            require(onboard.storage_mount(info, mount['target']) == mount, '已绑定的持久挂载已变化。')

    def exclusive(self, cid):
        for other in self.cli('ps', '--all', '--no-trunc', '--format', '{{.ID}}').splitlines():
            if other == cid:
                continue
            info = self.info(other)
            require(not equivalent_ref(info['ref'], self.b['imageRef']), '另一个容器使用相同镜像引用，无法安全切换。')
            for row in info.get('mounts') or []:
                if info['running'] and row.get('RW') and row['Type'] in ('volume', 'bind'):
                    mount = {'type': row['Type'], 'source': row.get('Name') if row['Type'] == 'volume' else row['Source']}
                    require(not any(same_storage(mount, value) for value in self.b['storage'].values()),
                            '另一个运行中的容器正在写入相同持久存储。')

    def unchanged_ref(self, expected):
        require(self.image(self.b['imageRef']) == expected, 'Compose 镜像标签已被其他操作修改。')

    def volume(self, name, task, *, create=False, resume=False):
        if create:
            if name in self.cli('volume', 'ls', '--format', '{{.Name}}').splitlines():
                require(resume, '隔离卷名已占用。')
            else:
                self.cli('volume', 'create', '--label', LABEL + '=' + task, name)
        label = self.cli('volume', 'inspect', '--format', '{{index .Labels "' + LABEL + '"}}', name)
        require(label == task, '快照卷与当前事务不匹配。')
        return {'type': 'volume', 'source': name}

    def copy(self, helper, source, target, kind, *, restore_digest=None):
        args = ['run', '--rm', '--pull=never', '--network', 'none', '--read-only', '--user', '0',
                '--security-opt', 'no-new-privileges', '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE',
                '--cap-add', 'FOWNER', '--cap-add', 'SETFCAP', '--env', 'BASH_ENV=', '--env', 'LD_PRELOAD=',
                '--env', 'LD_LIBRARY_PATH=', '--env', 'PATH=/usr/bin:/bin', '--entrypoint', '/bin/bash',
                '--mount', mount_arg(source, '/source', readonly=True)]
        if target:
            args += ['--mount', mount_arg(target, '/target')]
        host_times = bool(restore_digest and target and target['type'] == 'bind')
        mode = 'restore-bind' if host_times else 'restore' if restore_digest else 'copy' if target else 'fingerprint'
        value = self.cli(*args, helper, '-c', VOLUME_TASK, 'volume', mode, kind,
                         restore_digest or '', timeout=7200)
        require(ID.fullmatch(value), '存储快照校验失败。')
        if host_times:
            # Docker Desktop rounds directory mtimes to seconds on writes through a bind mount.
            self.restore_directory_times(helper, source, target, kind)
            require(self.copy(helper, target, None, kind) == value, '绑定目录恢复后与冷备不一致。')
        return value

    def restore_directory_times(self, helper, source, target, kind):
        raw = self.cli('run', '--rm', '--pull=never', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                       '--cap-add', 'DAC_OVERRIDE', '--user', '0', '--env', 'NODE_OPTIONS=', '--entrypoint', '/usr/local/bin/node',
                       '--mount', mount_arg(source, '/source', readonly=True), helper, '-e', DIRECTORY_TIMES, kind, timeout=1800)
        rows = json.loads(raw)
        require(isinstance(rows, list), '目录时间记录无效。')
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
        root = os.open(target['source'], flags)
        try:
            for encoded, timestamp in rows:
                rel = base64.b64decode(encoded, validate=True)
                parts = rel.split(b'/') if rel else []
                require(not set(parts) & {b'', b'.', b'..'} and re.fullmatch(r'-?[0-9]{1,20}', timestamp)
                        and not (kind == 'data' and parts[:1] == [b'dsh-source-updater']), '目录时间记录无效。')
                fd = os.dup(root)
                try:
                    for part in parts:
                        child = os.open(part, flags, dir_fd=fd)
                        os.close(fd)
                        fd = child
                    os.utime(fd, ns=(os.fstat(fd).st_atime_ns, int(timestamp)))
                finally:
                    os.close(fd)
        finally:
            os.close(root)

    def healthy(self, cid, image):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            info = self.info(cid)
            require(info['image'] == image and info['running'], 'DSH 容器已退出或镜像发生变化。')
            try:
                self.cli('exec', cid, 'node', '--input-type=module', '-e', WEB_CHECK,
                         str(self.b['containerPort']), timeout=12)
                return
            except onboard.SetupError:
                time.sleep(2)
        raise TransactionError('DSH Web 页面和升级插件未在限定时间内就绪。')

    def recreate(self):
        self.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', self.b['service'])
        cid = self.current()
        require(cid, 'Compose 未创建目标服务。')
        return cid

    def instance_binding(self, cid, image):
        info = self.bound(cid, image)
        info['npmVersion'] = onboard.npm_version(info, self.b['docker'], self.b['context'])
        new = onboard.candidate(info, self.b['docker'], self.b['context'], self.b['endpoint'], approved_compose=self.b)
        require(new and identity(new) == identity(self.b), '新实例的 Compose 或挂载与原绑定不同。')
        return new


def volume_name(task, generation, kind):
    require(re.fullmatch(r'[a-f0-9]{32}', task) and (generation in ('probe', 'cold', 'changes')
            or re.fullmatch(r'interrupted-[0-9a-f]{64}', generation))
            and kind in ('data', 'workspace'), '快照卷标识无效。')
    return 'dsh-upgrade-' + '-'.join((task, generation, kind))


def archive(d, j, generation):
    recorded = {}
    for kind, source in d.b['storage'].items():
        name = volume_name(j['task'], generation, kind)
        destination = d.volume(name, j['task'], create=True, resume=True)
        require(not d.cli('ps', '--filter', 'volume=' + name, '--format', '{{.ID}}'), '隔离快照卷已被其他容器占用。')
        digest = d.copy(j['oldImage'], source, None, kind)
        recorded[kind] = d.copy(j['oldImage'], source, destination, kind, restore_digest=digest)
    return recorded


def stopped(d, cid):
    require(d.current() == cid and (not cid or not d.info(cid)['running']), '服务已重新启动，停止写入存储。')


def stop(d, cid, boot):
    from portable_control import stop_if_idle
    started = d.inspect(cid, '{{json .State.StartedAt}}')
    if not stop_if_idle(d.instance_binding(cid, d.info(cid)['image']), cid, boot):
        return False
    for _ in range(65):
        old = d.inspect(cid, '{{json .State}}')
        require(old['StartedAt'] == started, '容器在关闭确认期间已重新启动。')
        if not old['Running']:
            require(old['Status'] == 'exited' and old['ExitCode'] == 0 and not old.get('OOMKilled'), '容器未正常退出。')
            return True
        time.sleep(0.5)
    raise TransactionError('未能确认原实例已正常退出。')


def idle_boot(d, cid):
    from portable_control import activate, status
    verified = d.instance_binding(cid, d.info(cid)['image'])
    require(activate(verified, cid), '此实例未启用宿主控制。')
    reply = status(verified, cid)
    require(isinstance(reply, dict) and isinstance(reply.get('busy'), bool) and reply.get('boot'), '无法核验宿主停机门禁。')
    return None if reply['busy'] else reply['boot']


def checked_inventory(binding, cid, old=None):
    from portable_control import activate, inventory
    require(activate(binding, cid), '当前实例未启用只读业务验收。')
    data = inventory(binding, cid, cuts(old) if old is not None else None)
    if old is None:
        cuts(data)
    else:
        assert_preserved(old, data)
    return data


def preflight(binding, state):
    d = Docker(binding, state)
    d.bound(binding['containerId'], binding['imageId'])
    d.unchanged_ref(binding['imageId'])
    d.exclusive(binding['containerId'])
    d.writable(binding['containerId'], binding['imageId'])


def validate(binding, state, image_id, sha):
    require(IMAGE.fullmatch(image_id) and SHA.fullmatch(sha), '候选镜像或源码提交无效。')
    (state / RECEIPT).unlink(missing_ok=True)
    preflight(binding, state)
    d = Docker(binding, state)
    require(d.image(image_id) == image_id, '候选镜像已变化。')
    ssh = d.ssh_keys(binding['containerId'])
    task = uuid.uuid4().hex
    helper = binding['imageId']
    baseline = checked_inventory(binding, binding['containerId'])
    volumes = []
    probe = None
    try:
        args = ['create', '--pull=never', '--label', LABEL + '=' + task, '--network', 'none', '--restart', 'no']
        for kind, source in binding['storage'].items():
            name = volume_name(task, 'probe', kind)
            snap = d.volume(name, task, create=True)
            volumes.append(name)
            d.copy(helper, source, snap, kind)
            args.extend(['--mount', mount_arg(snap, source['target'])])
        config = d.inspect(binding['containerId'], '{{json .Config}}')
        env = config.get('Env') or []
        require(all(isinstance(line, str) and '=' in line and '\n' not in line and '\r' not in line
                    and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', line.split('=', 1)[0]) for line in env),
                '容器环境无法安全复制到隔离验证环境。')
        entry = config.get('Entrypoint') or []
        with tempfile.NamedTemporaryFile(mode='w', dir=state) as file:
            file.write(''.join(line + '\n' for line in env))
            file.flush()
            args.extend(['--env-file', file.name, '--entrypoint', entry[0] if entry else ''])
            if config.get('User'):
                args.extend(['--user', config['User']])
            if config.get('WorkingDir'):
                args.extend(['--workdir', config['WorkingDir']])
            probe = d.cli(*args, image_id, *entry[1:], *(config.get('Cmd') or []))
        require(ID.fullmatch(probe), '无法识别隔离验证容器。')
        d.cli('start', probe)
        d.healthy(probe, image_id)
        require(d.ssh_keys(probe) == ssh, '隔离候选的 SSH 主机公钥与原实例不同。')
        from portable_control import activate, status
        evidence = d.inspect(probe, '[{{json (index .Config.Labels "' + LABEL + '")}},{{json .HostConfig.NetworkMode}},{{json .Mounts}}]')
        expected = {(source['target'], name) for source, name in zip(binding['storage'].values(), volumes)}
        require(evidence[0] == task and evidence[1] == 'none' and len(evidence[2]) == len(expected)
                and {(m['Destination'], m.get('Name')) for m in evidence[2] if m['Type'] == 'volume'} == expected,
                '隔离候选实例未使用预期的数据副本。')
        isolated = dict(binding, containerId=probe, imageId=image_id)
        require(activate(isolated, probe) and status(isolated, probe).get('boot'), '隔离实例升级服务没有就绪。')
        checked_inventory(isolated, probe, baseline)
        d.bound(binding['containerId'], helper)
        save(state / RECEIPT, dict(sha=sha, image=image_id, oldImage=helper, oldId=binding['containerId'],
                                   binding=identity(binding), ssh=ssh, inventory=baseline))
    finally:
        if probe and ID.fullmatch(probe):
            label = d.inspect(probe, '{{json (index .Config.Labels "' + LABEL + '")}}')
            require(label == task, '隔离容器标识已变化。')
            d.cli('rm', '--force', '--volumes', probe)
        for name in volumes:
            d.volume(name, task)
            d.cli('volume', 'rm', name)


def set_phase(d, journal, phase):
    journal['phase'] = phase
    save(d.state / JOURNAL, journal)


def backup_receipt(state, journal, result):
    root = state / 'backups'
    root.mkdir(mode=0o700, exist_ok=True)
    require(root.is_dir() and not root.is_symlink(), '宿主备份收据目录无效。')
    task = journal['task']
    generations = {generation: {kind: {'volume': volume_name(task, generation, kind), 'digest': digest}
                                for kind, digest in journal[generation].items()}
                   for generation in ('cold', 'changes') if journal[generation]}
    for generation, values in journal.get('additional', {}).items():
        generations[generation] = {k: {'volume': volume_name(task, generation, k), 'digest': v} for k, v in values.items()}
    save(root / (task + '.json'), dict(task=task, result=result, source=journal['sha'], oldImage=journal['oldImage'],
                                      newImage=journal['newImage'], rollbackTag='dsh-upgrade-rollback:' + task,
                                      generations=generations))


def restart_policy(d, cid):
    policy = d.inspect(cid, '{{json .HostConfig.RestartPolicy}}')
    name = policy.get('Name')
    require(name in ('no', 'always', 'unless-stopped', 'on-failure'), '无法验证当前容器重启策略。')
    return f"on-failure:{policy['MaximumRetryCount']}" if name == 'on-failure' and policy['MaximumRetryCount'] else name


def deploy(binding, state, image_id, sha):
    d = Docker(binding, state)
    require(read(state / JOURNAL) is None, '上一次部署仍需恢复。')
    expected = dict(sha=sha, image=image_id, oldImage=binding['imageId'], oldId=binding['containerId'],
                    binding=identity(binding), ssh=d.ssh_keys(binding['containerId']))
    record = read(state / RECEIPT)
    require(IMAGE.fullmatch(image_id) and SHA.fullmatch(sha) and isinstance(record, dict) and
            all(record.get(k) == v for k, v in expected.items()) and isinstance(record.get('inventory'), dict),
            '需要针对当前服务和候选镜像重新进行隔离验证。')
    preflight(binding, state)
    cid, old = binding['containerId'], binding['imageId']
    boot = idle_boot(d, cid)
    if boot is None:
        return 75
    checked_inventory(binding, cid, record['inventory'])
    baseline = checked_inventory(binding, cid)
    checked = d.writable(cid, old)
    task = uuid.uuid4().hex
    journal = dict(task=task, binding=identity(binding), oldId=cid, oldImage=old, newImage=image_id,
                   restart=restart_policy(d, cid), sha=sha, composeHash=d.config_hash(cid),
                   storageIds=d.storage_ids(old), inventory=baseline, ssh=expected['ssh'], cold={}, changes={}, additional={})
    d.cli('image', 'tag', old, 'dsh-upgrade-rollback:' + task)
    set_phase(d, journal, 'preparing')
    try:
        d.cli('update', '--restart=no', cid)
        if not stop(d, cid, boot):
            d.cli('update', '--restart=' + journal['restart'], cid)
            (state / JOURNAL).unlink()
            return 75
        d.bound(cid, old, running=False)
        stopped(d, cid)
        d.writable(cid, old, checked)
        d.exclusive(cid)
        set_phase(d, journal, 'stopped')
        require(d.storage_ids(old) == journal['storageIds'], '冷备开始前持久存储已被更换。')
        journal['cold'] = archive(d, journal, 'cold')
        d.bound(cid, old, running=False)
        stopped(d, cid)
        d.unchanged_ref(old)
        d.exclusive(cid)
        set_phase(d, journal, 'switching')
        d.cli('image', 'tag', image_id, binding['imageRef'])
        next_id = d.recreate()
        d.bound(next_id, image_id)
        d.healthy(next_id, image_id)
        require(d.ssh_keys(next_id) == expected['ssh'], '新实例 SSH 主机公钥与原实例不同。')
        idle_boot(d, next_id)
        next_binding = d.instance_binding(next_id, image_id)
        checked_inventory(next_binding, next_id, baseline)
        save(state / 'binding.json', next_binding)
        set_phase(d, journal, 'verified')
        backup_receipt(state, journal, 'upgraded')
        (state / JOURNAL).unlink()
        return 0
    except Exception:
        verified = (read(state / JOURNAL) or {}).get('phase') == 'verified'
        recover(binding, state)
        return 0 if verified else 78


def recover(binding, state):
    journal = read(state / JOURNAL)
    if journal is None:
        return
    d = Docker(binding, state)
    require(journal.get('binding') == identity(binding) and
            all(IMAGE.fullmatch(journal[key]) for key in ('oldImage', 'newImage')) and
            ID.fullmatch(journal['oldId']) and re.fullmatch(r'[0-9a-f]{32}', journal['task']),
            '事务记录与目标绑定不一致。')
    task, old, new = journal['task'], journal['oldImage'], journal['newImage']
    require(d.image('dsh-upgrade-rollback:' + task) == old and ID.fullmatch(journal['composeHash']) and
            onboard.compose_service(binding, binding['storage'], journal['composeHash']) == binding['composeConfigSHA256'],
            '回滚镜像或 Compose 配置已变化，保留备份。')
    if journal['phase'] == 'verified':
        backup_receipt(state, journal, 'upgraded')
        (state / JOURNAL).unlink()
        return
    cid = d.current()
    d.exclusive(cid)
    require(d.storage_ids(old) == journal['storageIds'], '持久目录或卷的物理身份已变化，保留全部备份。')
    ref = d.image(binding['imageRef'])
    require(ref in (old, new), '服务镜像标签被独立修改，保留现有状态。')
    if journal['phase'] in ('preparing', 'stopped'):
        require(cid == journal['oldId'] and ref == old, '原实例已改变，需重新核实。')
        info = d.bound(cid, old, running=False)
        d.cli('update', '--restart=' + journal['restart'], cid)
        if not info['running']:
            d.cli('start', cid)
        d.healthy(cid, old)
        checked_inventory(d.instance_binding(cid, old), cid, journal['inventory'])
    else:
        require(journal['phase'] in ('switching', 'restoring', 'restarting') and
                set(journal['cold']) == {'data', 'workspace'}, '恢复状态缺少完整的冷备。')
        if cid:
            info = d.info(cid)
            require(info['image'] in (old, new), '服务已经切换到无关镜像。')
            d.bound(cid, info['image'], running=False)
            if journal['phase'] == 'restarting' and info['running']:
                require(info['image'] == old, '恢复启动期间另一实例已运行，保留现有数据。')
            if info['running'] and journal['phase'] != 'restarting':
                require(journal['phase'] != 'restoring', '恢复期间已有实例再次运行，保留新数据和原备份。')
                boot = idle_boot(d, cid)
                require(boot is not None, '服务当前有工作，已延后恢复。')
                checked = d.writable(cid, info['image'])
                policy = restart_policy(d, cid)
                d.cli('update', '--restart=no', cid)
                try:
                    require(stop(d, cid, boot), '服务当前有工作，已延后恢复。')
                except Exception:
                    d.cli('update', '--restart=' + policy, cid)
                    raise
                d.writable(cid, info['image'], checked)
            elif not info['running'] and journal['phase'] != 'restarting':
                d.writable(cid, info['image'])
        if journal['phase'] != 'restarting':
            stopped(d, cid)
            d.exclusive(cid)
            if journal['phase'] == 'switching':
                journal['changes'] = archive(d, journal, 'changes')
                set_phase(d, journal, 'restoring')
            # Both generations are verified before replacing either live tree.
            for kind in binding['storage']:
                for generation in ('cold', 'changes'):
                    vol = d.volume(volume_name(task, generation, kind), task)
                    require(d.copy(old, vol, None, kind) == journal[generation][kind], '恢复副本校验失败。')
            for kind, target in binding['storage'].items():
                stopped(d, cid)
                require(d.storage_ids(old) == journal['storageIds'], '持久存储已被更换，停止恢复。')
                current = d.copy(old, target, None, kind)
                if current not in (journal['changes'][kind], journal['cold'][kind]):
                    # Preserve even an incomplete previous restore or an external edit before replacing the tree.
                    generation = 'interrupted-' + current
                    extra = d.volume(volume_name(task, generation, kind), task, create=True, resume=True)
                    d.copy(old, target, extra, kind, restore_digest=current)
                    journal['additional'].setdefault(generation, {})[kind] = current
                    set_phase(d, journal, 'restoring')
                cold = d.volume(volume_name(task, 'cold', kind), task)
                d.copy(old, cold, target, kind, restore_digest=journal['cold'][kind])
            d.unchanged_ref(ref)
            d.cli('image', 'tag', old, binding['imageRef'])
            set_phase(d, journal, 'restarting')
        d.unchanged_ref(old)
        if not cid or not d.info(cid)['running'] or d.info(cid)['image'] != old:
            cid = d.recreate()
        d.bound(cid, old)
        d.healthy(cid, old)
        idle_boot(d, cid)
        require(d.ssh_keys(cid) == journal['ssh'], '恢复后的 SSH 主机公钥发生变化。')
        restored = d.instance_binding(cid, old)
        checked_inventory(restored, cid, journal['inventory'])
        save(state / 'binding.json', restored)
    backup_receipt(state, journal, 'restored')
    (state / JOURNAL).unlink()
