import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host'))
import portable_transaction as tx

OLD = 'sha256:' + '1' * 64
NEW = 'sha256:' + '2' * 64
CID = 'a' * 64
SHA = 'b' * 40


class FakeDocker:
    def __init__(self, state):
        self.state = state
        self.b = {key: 'demo' for key in tx.STABLE}
        self.b.update(containerId=CID, imageId=OLD, imageRef='example-demo:web', containerPort=3080,
                      storage={'data': dict(type='volume', source='demo-data', target='/app/data'),
                               'workspace': dict(type='volume', source='demo-work', target='/workspace')})
        self.cid = CID
        self.running = True
        self.image_id = OLD
        self.tags = {self.b['imageRef']: OLD}
        self.volumes = {'demo-data': 'initial data', 'demo-work': 'project'}
        self.policy = 'unless-stopped'
        self.reject_new = False
        self.changed = []

    def current(self):
        return self.cid

    def config_hash(self, cid):
        assert self.cid == cid
        return 'f' * 64

    def info(self, cid):
        assert cid == self.cid
        return {'running': self.running, 'id': self.cid, 'image': self.image_id}

    def bound(self, cid, image, *, running=True):
        assert cid == self.cid and image == self.image_id and (not running or self.running)
        return self.info(cid)

    def instance_binding(self, cid, image):
        self.bound(cid, image)
        return dict(self.b, containerId=cid, imageId=image)

    def image(self, ref):
        return self.tags.get(ref, ref)

    def unchanged_ref(self, image):
        assert self.tags[self.b['imageRef']] == image

    def writable(self, cid, image, previous=None):
        assert self.cid == cid
        return {}

    def ssh_keys(self, cid):
        assert self.cid == cid
        return None

    def storage_ids(self, helper):
        assert helper == OLD
        return {'data': 'physical data', 'workspace': 'physical workspace'}

    def exclusive(self, cid):
        assert self.cid == cid

    def inspect(self, cid, template):
        assert cid == self.cid
        if 'RestartPolicy' in template:
            return {'Name': self.policy, 'MaximumRetryCount': 0}
        raise AssertionError(template)

    def volume(self, name, task, *, create=False, resume=False):
        assert task in name
        if create:
            self.volumes.setdefault(name, '')
        assert name in self.volumes
        return {'type': 'volume', 'source': name}

    def copy(self, helper, source, target, kind, *, restore_digest=None):
        assert helper == OLD
        value = self.volumes[source['source']]
        digest = hashlib.sha256(value.encode()).hexdigest()
        assert restore_digest is None or digest == restore_digest
        if target:
            self.volumes[target['source']] = value
        return digest

    def cli(self, *args, **kwargs):
        if args[:2] == ('image', 'tag'):
            self.tags[args[3]] = args[2]
        elif args[0] == 'update':
            assert args[2] == self.cid
            self.policy = args[1].split('=', 1)[1]
        elif args[0] == 'ps':
            return ''
        elif args[0] == 'start':
            assert args[1] == self.cid
            self.running = True
        else:
            raise AssertionError(args)
        self.changed.append(args)
        return ''

    def recreate(self):
        self.running = True
        self.image_id = self.tags[self.b['imageRef']]
        self.cid = ('c' if self.image_id == NEW else 'd') * 64
        self.policy = 'unless-stopped'
        return self.cid

    def healthy(self, cid, image):
        self.bound(cid, image)
        if self.reject_new and image == NEW:
            self.volumes['demo-data'] = 'new write before failure'
            self.volumes['demo-work'] = 'edited project'
            raise tx.TransactionError('synthetic readiness failure')


class TransactionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.d = FakeDocker(self.state)
        self.b = self.d.b
        for context in (
                patch.object(tx, 'Docker', return_value=self.d),
                patch.object(tx, 'checked_inventory', return_value={'test':'synthetic'}),
                patch.object(tx, 'idle_boot', return_value='boot'),
                patch.object(tx, 'stop', side_effect=self.stop),
                patch.object(tx.onboard, 'compose_service', return_value=self.b['composeConfigSHA256'])):
            context.start()
            self.addCleanup(context.stop)
        tx.save(self.state / tx.RECEIPT, dict(sha=SHA, image=NEW, oldImage=OLD, oldId=CID,
                                             binding=tx.identity(self.b), ssh=None, inventory={'test':'synthetic'}))

    def stop(self, docker, cid, boot):
        assert cid == docker.cid and boot == 'boot'
        docker.running = False
        return True

    def test_fresh_receipt_verified_cold_copies_and_successful_binding_update(self):
        self.assertEqual(tx.deploy(self.b, self.state, NEW, SHA), 0)
        self.assertFalse((self.state / tx.JOURNAL).exists())
        self.assertEqual(tx.read(self.state / 'binding.json')['containerId'], 'c' * 64)
        self.assertEqual(self.d.tags[self.b['imageRef']], NEW)
        self.assertEqual(sorted(value for key, value in self.d.volumes.items() if '-cold-' in key),
                         ['initial data', 'project'])

    def test_unhealthy_new_service_preserves_both_generations_and_restores_both_mounts(self):
        self.d.reject_new = True
        self.assertEqual(tx.deploy(self.b, self.state, NEW, SHA), 78)
        self.assertEqual(self.d.tags[self.b['imageRef']], OLD)
        self.assertEqual(self.d.volumes['demo-data'], 'initial data')
        self.assertEqual(self.d.volumes['demo-work'], 'project')
        self.assertEqual(sorted(value for key, value in self.d.volumes.items() if '-changes-' in key),
                         ['edited project', 'new write before failure'])
        self.assertEqual(tx.read(self.state / 'binding.json')['imageId'], OLD)

    def test_missing_gate_busy_gate_receipt_mismatch_and_tag_drift_do_not_switch(self):
        with patch.object(tx, 'idle_boot', side_effect=RuntimeError('gate absent')):
            with self.assertRaises(RuntimeError):
                tx.deploy(self.b, self.state, NEW, SHA)
        with patch.object(tx, 'idle_boot', return_value=None):
            self.assertEqual(tx.deploy(self.b, self.state, NEW, SHA), 75)
        with self.assertRaises(tx.TransactionError):
            tx.deploy(self.b, self.state, NEW, 'a' * 40)
        self.assertEqual(self.d.changed, [])
        with patch.object(tx, 'stop', return_value=False):
            self.assertEqual(tx.deploy(self.b, self.state, NEW, SHA), 75)
        self.assertTrue(self.d.running)
        self.assertEqual(self.d.policy, 'unless-stopped')
        self.assertEqual(self.d.tags[self.b['imageRef']], OLD)

    def test_interrupted_generation_restore_recovers_and_keeps_new_writes(self):
        class Crash(BaseException):
            pass
        self.d.reject_new = True
        copy = self.d.copy
        def interrupt(helper, source, target, kind, **opts):
            if target and target['source'] == 'demo-work':
                raise Crash()
            return copy(helper, source, target, kind, **opts)
        with patch.object(self.d, 'copy', side_effect=interrupt):
            with self.assertRaises(Crash):
                tx.deploy(self.b, self.state, NEW, SHA)
        self.assertEqual(tx.read(self.state / tx.JOURNAL)['phase'], 'restoring')
        tx.recover(self.b, self.state)
        self.assertTrue(self.d.running)
        self.assertEqual(self.d.volumes['demo-work'], 'project')
        self.assertIn('edited project', self.d.volumes.values())
        tx.recover(self.b, self.state)

    def test_interrupted_recovery_refuses_to_overwrite_an_independently_changed_tag(self):
        class Crash(BaseException):
            pass
        with patch.object(self.d, 'recreate', side_effect=Crash):
            with self.assertRaises(Crash):
                tx.deploy(self.b, self.state, NEW, SHA)
        self.d.tags[self.b['imageRef']] = 'sha256:' + '9' * 64
        with self.assertRaises(tx.TransactionError):
            tx.recover(self.b, self.state)
        self.assertTrue((self.state / tx.JOURNAL).is_file())
        self.assertEqual(self.d.volumes['demo-data'], 'initial data')


class GuardTests(unittest.TestCase):
    def test_writable_layer_requires_a_reason_for_every_changed_leaf(self):
        allowed = [{'Path':'/tmp','Kind':0},{'Path':'/tmp/dsh-spill-Ab1De2','Kind':1},
                   {'Path':'/app','Kind':0},{'Path':'/app/data','Kind':1}]
        self.assertEqual(tx.allowed_changes(allowed, {'/app/data'}), {'/tmp/dsh-spill-Ab1De2':'runtime'})
        for unknown in ('/etc/ssh/ssh_host_ed25519_key', '/root/.ssh/authorized_keys', '/tmp/dsh-spill-Ab1De2/chat', '/opt/packages/tool'):
            with self.subTest(unknown=unknown), self.assertRaises(tx.TransactionError) as context:
                tx.allowed_changes(allowed+[{'Path':unknown,'Kind':1}], {'/app/data'})
            self.assertNotIn(unknown,str(context.exception))

    def test_reject_extra_mounts_shared_stopped_tag_and_overlapping_writable_bind(self):
        docker = object.__new__(tx.Docker)
        docker.b = {'imageRef': 'test-project-dsh:latest',
                    'storage': {'data': {'type': 'bind', 'source': '/one', 'target': '/app/data'},
                                'workspace': {'type': 'volume', 'source': 'work', 'target': '/workspace'}}}
        with self.assertRaises(tx.TransactionError):
            docker.mounts({'mounts': [{}, {}, {}]})
        other = {'ref': 'test-project-dsh', 'running': False, 'mounts': []}
        with patch.object(docker, 'cli', return_value='f' * 64), patch.object(docker, 'info', return_value=other):
            with self.assertRaises(tx.TransactionError):
                docker.exclusive(CID)
            other.update(ref='other', running=True, mounts=[{'Type': 'bind', 'Source': '/one/nested', 'RW': True}])
            with self.assertRaises(tx.TransactionError):
                docker.exclusive(CID)
            other['mounts'][0]['RW'] = False
            docker.exclusive(CID)

    @unittest.skipUnless(os.environ.get('DSH_TRANSACTION_ARCHIVE_TEST') == '1', 'requires an explicitly enabled isolated Docker run')
    def test_gnu_archive_on_private_tmpfs_keeps_bytes_permissions_links_and_mailbox(self):
        program = '''set -euo pipefail
cat > /checks/copy <<'DSH_ARCHIVE_TASK'
''' + tx.VOLUME_TASK + '''\nDSH_ARCHIVE_TASK
mkdir /source/nested /source/dsh-source-updater
chmod 0751 /source
echo original > /source/nested/a
ln /source/nested/a /source/nested/b
ln -s 'nested/a' /source/link
echo transient > /source/dsh-source-updater/marker
hash=$(bash /checks/copy copy data)
[[ ! -e /target/dsh-source-updater && /target/nested/a -ef /target/nested/b && -L /target/link ]]
mkdir /target/dsh-source-updater
echo kept > /target/dsh-source-updater/status
echo replacement > /target/nested/a
bash /checks/copy restore data "$hash" >/dev/null
[[ $(cat /target/nested/a) == original && $(cat /target/dsh-source-updater/status) == kept ]]
mkfifo /source/unsupported
if bash /checks/copy fingerprint data >/dev/null 2>&1; then exit 1; fi
'''
        proc = subprocess.run(['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--read-only',
                               '--tmpfs', '/source:rw,size=8m', '--tmpfs', '/target:rw,size=8m',
                               '--tmpfs', '/checks:rw,size=1m', '-i', '--entrypoint', '/bin/bash',
                               'node:24.22.3-bookworm', '-s'], input=program, text=True, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[:1200])

    def test_ssh_terminals_connections_and_unverifiable_proc_are_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'net').mkdir()
            header = 'sl local_address rem_address st tx rx timer retr uid inode\n'
            (root / 'net/tcp').write_text(header)
            process = root / '1'
            process.mkdir()
            def idle(strict=False):
                js = (ROOT / 'ssh-idle.js').as_uri()
                result = subprocess.run(['node', '--input-type=module', '-e',
                    f'import {{sshIdle}} from {json.dumps(js)}; process.exit(sshIdle(process.argv[1], process.argv[2] === "yes") ? 0 : 1)',
                    str(root), 'yes' if strict else 'no'], capture_output=True)
                return result.returncode == 0
            (process / 'stat').write_text('1 (init) S 0 1 1 0 1 2')
            self.assertTrue(idle())
            (process / 'stat').write_text('1 (shell) S 0 1 1 9 1 2')
            self.assertFalse(idle())
            (process / 'stat').write_text('1 (sshd-session) S 0 1 1 0 1 2')
            self.assertFalse(idle())
            (process / 'stat').write_text('1 (sshd-session) Z 0 1 1 0 1 2')
            self.assertTrue(idle())
            (process / 'stat').write_text('1 (sshd) S 0 1 1 0 1 2')
            descriptors = process / 'fd'
            descriptors.mkdir()
            (descriptors / '4').symlink_to('socket:[42]')
            listen = '0: 00000000:08AE 00000000:0000 0A 0 0 0 0 0 42\n'
            item = '1: 00000000:08AE 00000000:1234 01 0 0 0 0 0 55\n'
            (root / 'net/tcp').write_text(header + listen + item)
            self.assertFalse(idle(True))
            (root / 'net/tcp').write_text(header + listen)
            self.assertTrue(idle(True))
            descriptors.chmod(0)
            try:
                if os.getuid() != 0:
                    self.assertTrue(idle())
                    self.assertFalse(idle(True))
            finally:
                descriptors.chmod(0o700)
            (root / 'net/tcp').write_text('invalid')
            self.assertFalse(idle())


if __name__ == '__main__':
    unittest.main()
