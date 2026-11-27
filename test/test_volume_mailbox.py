import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
from portable_control import DOCKER
from volume_mailbox import VolumeMailbox


class MailboxIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name).resolve()
        self.mail = VolumeMailbox(self.state)
        mount = dict(type='volume',source='owned-queue',target='/app/data')
        self.binding = dict(docker=DOCKER, endpoint='unix:///var/run/docker.sock', containerId='a'*64, imageId='sha256:'+'b'*64,
                            project='fixture',service='web',storage={'data':mount})
        (self.state/'binding.json').write_text(json.dumps(self.binding))
        self.info = dict(id=self.binding['containerId'],image=self.binding['imageId'],running=True,
                         project='fixture',service='web',mounts=[dict(Type='volume',Destination='/app/data',Name='owned-queue',RW=True)])
        self.facts = ['owned-queue','2020-01-01T00:00:00Z','local','local','fixture','queue']

    def test_existing_container_and_volume_creation_identity_are_both_required(self):
        with patch.object(self.mail, 'inspect',side_effect=[self.info,self.facts]) as inspect:
            self.assertEqual(self.mail.verify(self.binding,[]),'a'*64)
            self.assertEqual(inspect.call_args_list[1].args[1:3],('volume','inspect'))
        self.facts[1] = '2020-02-01T00:00:00Z'
        with patch.object(self.mail, 'inspect',side_effect=[self.info,self.facts]), patch('volume_mailbox.subprocess.Popen') as start:
            with self.assertRaisesRegex(OSError,'已被替换'):
                self.mail.open()
            start.assert_not_called()
        self.info['running']=False
        with patch.object(self.mail, 'inspect',return_value=self.info) as inspect, patch('volume_mailbox.subprocess.Popen') as start:
            with self.assertRaisesRegex(OSError,'已停止'):
                self.mail.open()
            self.assertEqual(inspect.call_count,1)
            start.assert_not_called()

    def test_wrong_mount_or_missing_volume_fails_before_start(self):
        self.info['mounts'][0]['Name']='unrelated'
        with patch.object(self.mail,'inspect',return_value=self.info),patch('volume_mailbox.subprocess.Popen') as start:
            with self.assertRaisesRegex(OSError,'挂载与批准'):
                self.mail.open()
            start.assert_not_called()
        self.info['mounts'][0]['Name']='owned-queue'
        with patch.object(self.mail,'inspect',side_effect=[self.info,OSError('missing')]),patch('volume_mailbox.subprocess.Popen') as start:
            with self.assertRaises(OSError):
                self.mail.open()
            self.assertFalse((self.state/'mailbox-volume.sha256').exists())
            start.assert_not_called()
