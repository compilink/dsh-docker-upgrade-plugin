"""Address the selected Docker Desktop container through its local Unix socket."""
import json
import os
from pathlib import Path
import re
import subprocess
import time


DOCKER = '/Applications/Docker.app/Contents/Resources/bin/docker'
ROOT = Path(__file__).resolve().parent
CONTAINER_ID = re.compile(r'[0-9a-f]{64}')


def docker_cli(binding):
    endpoint = binding['endpoint']
    if (binding['docker'] != DOCKER or endpoint not in {'unix:///var/run/docker.sock',
            'unix://' + str(Path.home() / '.docker/run/docker.sock')}):
        raise ValueError('宿主绑定没有指向本机 Docker Desktop。')
    return [DOCKER, '--host', endpoint]


def node_client(binding, container_id, client, arguments, payload=None, timeout=15):
    if (not CONTAINER_ID.fullmatch(container_id) or binding['containerId'] != container_id
            or client != 'control-client.cjs'):
        raise ValueError('容器身份与已确认的部署不一致。')
    # Only these bundled, fixed operations run with Docker's existing authority.
    program = (ROOT / client).read_text()
    if arguments[0] in ('status', 'stop'):
        check = ROOT / 'ssh-idle.js'
        payload = (check if check.is_file() else ROOT.parent / 'ssh-idle.js').read_bytes()
    args = [*docker_cli(binding), 'exec', '-i', '--user', '0', '--workdir', '/',
            '--env', 'NODE_OPTIONS=', '--env', 'NODE_PATH=', '--env', 'LD_PRELOAD=',
            container_id, '/usr/local/bin/node', '--input-type=commonjs', '-e', program,
            '--', 'dsh-portable', *arguments]
    proc = subprocess.run(args, input=payload, capture_output=True, timeout=timeout,
                          env={k:v for k,v in os.environ.items() if not k.startswith('DOCKER_')})
    limit = 8 * 1024 * 1024 if arguments[0] == 'inventory' else 16384
    # A foreground service can exit immediately after the root client flushed its stop receipt.
    # The transaction independently verifies the original boot, container exit status and storage before switching.
    has_stop_receipt = arguments[0] == 'stop' and proc.stdout.endswith(b'\n')
    if (proc.returncode and not has_stop_receipt) or len(proc.stdout) > limit:
        raise RuntimeError('容器内的升级服务没有返回有效确认。')
    return json.loads(proc.stdout)


def activate(binding, container_id):
    return node_client(binding, container_id, 'control-client.cjs', ['activate'])['active']


def status(binding, container_id):
    # The already-running Web plugin periodically discovers a native installation.
    for attempt in range(5):
        try:
            return node_client(binding, container_id, 'control-client.cjs', ['status'])
        except (RuntimeError, subprocess.TimeoutExpired):
            if attempt == 4:
                raise
            time.sleep(0.6)


def stop_if_idle(binding, container_id, boot):
    # Never retry an unacknowledged stop: the first request may have taken effect.
    receipt = node_client(binding, container_id, 'control-client.cjs', ['stop', boot])
    if receipt.get('boot') != boot or type(receipt.get('accepted')) is not bool:
        raise RuntimeError('无法核验原实例的停机确认。')
    return receipt['accepted']


def inventory(binding, container_id, cuts=None):
    payload = json.dumps(cuts, separators=(',', ':')).encode('ascii')
    if len(payload) > 4 * 1024 * 1024 - 1024:
        raise ValueError('会话校验请求超过单次容量。')
    return node_client(binding, container_id, 'control-client.cjs', ['inventory'], payload, timeout=130)
