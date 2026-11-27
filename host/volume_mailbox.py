"""Exchange bounded queue messages as the Web user inside the bound DSH container."""
import json
import hashlib
import os
from pathlib import Path
import re
import select
import subprocess
import threading
import time

from portable_control import ROOT, docker_cli
from install import atomic_file
from onboard import storage_mount

INBOX = "/app/data/dsh-source-updater"
INSPECT = ('{"id":{{json .Id}},"running":{{json .State.Running}},"image":{{json .Image}},'
           '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
           '"service":{{json (index .Config.Labels "com.docker.compose.service")}},"mounts":{{json .Mounts}}}')
VOLUME = ('[{{json .Name}},{{json .CreatedAt}},{{json .Driver}},{{json .Scope}},'
          '{{json (index .Labels "com.docker.compose.project")}},{{json (index .Labels "com.docker.compose.volume")}}]')


class VolumeMailbox:
    def __init__(self, state):
        self.state = state
        self.process = None
        self.lock = threading.RLock()
        self.pending = {}
        self.last_failure = 0
        self.container_id = None

    def inspect(self, docker, *args):
        proc = subprocess.run([*docker, *args], capture_output=True, timeout=8)
        if proc.returncode or len(proc.stdout) > 65536:
            raise OSError('无法核实绑定的 DSH 资源。')
        return json.loads(proc.stdout)

    def verify(self, binding, docker):
        cid = binding['containerId']
        mount = binding['storage']['data']
        if (mount['target'] != '/app/data' or mount['type'] not in ('bind', 'volume')
                or not re.fullmatch(r'[a-f0-9]{64}', cid)):
            raise ValueError('宿主信箱绑定无效。')
        info = self.inspect(docker, 'inspect', '--type', 'container', '--format', INSPECT, cid)
        if (not info['running'] or info['id'] != cid or info['image'] != binding['imageId'] or
                info['project'] != binding['project'] or info['service'] != binding['service'] or
                storage_mount(info, '/app/data') != mount):
            raise OSError('DSH 实例已停止，或实际挂载与批准的部署不同。')
        if mount['type'] == 'volume':
            name = mount['source']
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', name):
                raise ValueError('命名卷名称无效。')
            facts = self.inspect(docker, 'volume', 'inspect', '--format', VOLUME, name)
            if (not isinstance(facts, list) or len(facts) != 6 or facts[0] != name or not facts[1] or
                    not facts[2] or facts[3] != 'local'):
                raise OSError('无法确认已存在的本机命名卷。')
            marker = self.state / 'mailbox-volume.sha256'
            expected = hashlib.sha256(json.dumps(facts, separators=(',', ':')).encode()).hexdigest().encode()
            if marker.exists():
                if marker.is_symlink() or marker.read_bytes() != expected:
                    raise OSError('命名卷已被替换，需重新确认宿主绑定。')
            else:
                # The verified, still-existing container references this volume before it is first recorded.
                atomic_file(marker, expected)
        return cid

    def open(self):
        binding = json.loads((self.state / 'binding.json').read_bytes())
        if (self.process is not None and self.process.poll() is None
                and self.container_id == binding['containerId']):
            return
        self.close()
        docker = docker_cli(binding)
        cid = self.verify(binding, docker)
        source = (ROOT / 'mailbox-client.cjs').read_text()
        # Running in the already-mounted target never creates a named volume. While it is down,
        # the worker keeps its latest snapshot privately and reconnects after transaction recovery.
        args = [*docker, 'exec', '-i', '--user', 'node', '--workdir', '/', '--env', 'NODE_OPTIONS=',
                '--env', 'NODE_PATH=', '--env', 'LD_PRELOAD=', cid, '/usr/local/bin/node', '--permission',
                '--allow-fs-read=' + INBOX, '--allow-fs-write=' + INBOX, '--allow-fs-read=/proc/self/fd',
                '--input-type=commonjs', '-e', source]
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        self.container_id = cid
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)

    def close(self):
        with self.lock:
            child = self.process
            self.process = None
            self.container_id = None
            if child is None:
                return
            child.stdin.close()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            child.stdout.close()

    def call(self, op, **values):
        self.open()
        message = json.dumps(dict(op=op, **values),separators=(',', ':')).encode() + b'\n'
        if len(message) > 16384:
            raise ValueError('状态过长。')
        deadline = time.monotonic() + 12
        input_fd, output_fd = self.process.stdin.fileno(), self.process.stdout.fileno()
        while message:
            delay = deadline - time.monotonic()
            if delay <= 0 or not select.select([], [input_fd], [], delay)[1]:
                raise OSError('数据信箱通信超时。')
            message = message[os.write(input_fd, message):]
        payload = bytearray()
        while not payload.endswith(b'\n'):
            delay = deadline - time.monotonic()
            if delay <= 0 or not select.select([output_fd], [], [], delay)[0]:
                raise OSError('数据信箱通信超时。')
            part = os.read(output_fd, 16385 - len(payload))
            if not part or len(part) + len(payload) > 16384:
                raise OSError('无法读取 DSH 专用信箱。')
            payload.extend(part)
        response = json.loads(payload)
        if 'result' not in response:
            raise OSError('无法读写 DSH 升级状态。')
        return response['result']

    def exchange(self, op, **values):
        with self.lock:
            if time.monotonic() - self.last_failure < 4:
                raise OSError('暂时无法访问 DSH 数据卷。')
            try:
                for key, value in list(self.pending.items()):
                    self.call('write', name=key, value=value)
                    self.pending.pop(key)
                if op:
                    return self.call(op, **values)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                self.last_failure = time.monotonic()
                self.close()
                raise OSError('暂时无法访问 DSH 数据卷。') from error

    def write(self, name, value):
        if name not in ('worker.json','status.json'):
            raise ValueError('不支持的状态文件。')
        with self.lock:
            self.pending[name] = value.copy()
            try:
                self.exchange(None)
            except OSError:
                # SQLite is authoritative. Deliver the latest public snapshot when Docker is available.
                pass

    def request(self):
        return self.exchange('request')

    def acknowledge(self, identity):
        self.exchange('ack', identity=identity)
