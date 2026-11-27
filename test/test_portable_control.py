"""Exercise the actual root-only activation and unprivileged Web peer inside Docker Desktop."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'host'))
import onboard
import portable_control as control


@unittest.skipUnless(os.environ.get('DSH_TEST_PORTABLE_DOCKER') == '1', 'requires local Docker Desktop')
class DockerControlTests(unittest.TestCase):
    def test_activation_status_stop(self):
        context, endpoint = onboard.local_context()
        docker = [onboard.DOCKER, '--host', endpoint]
        name = 'dsh-control-test-' + uuid4().hex[:12]
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            path = Path(temp)
            path.chmod(0o755)
            shutil.copyfile(ROOT / 'portable-control.js', path / 'control.mjs')
            script = ("import {mountPortableControl} from './control.mjs';import{randomUUID}from'node:crypto';"
                      "process.on('SIGTERM',()=>process.exit(0));mountPortableControl(randomUUID(),()=>false,({cuts})=>({count:Object.keys(cuts??{}).length}));"
                      "setInterval(()=>{},30000);")
            (path / 'start.mjs').write_text(script)
            cid = subprocess.check_output([*docker, 'run','-d','--name',name,'--pull','never','--user','node',
                                           '--network','none','--read-only','--tmpfs','/run:mode=0755',
                                           '--mount',f'type=bind,src={path},dst=/fixture,readonly',
                                           'node:24.22.1-alpine','node','/fixture/start.mjs'],text=True).strip()
            try:
                binding = dict(context=context,endpoint=endpoint,docker=onboard.DOCKER,containerId=cid)
                self.assertTrue(control.activate(binding,cid))
                state = control.status(binding,cid)
                self.assertFalse(state['busy'])
                self.assertEqual(control.inventory(binding,cid), {'count': 0})
                self.assertEqual(control.inventory(binding,cid,{'a' * 64: 2}), {'count': 1})
                with self.assertRaises(RuntimeError):
                    control.stop_if_idle(binding,cid,str(uuid4()))
                self.assertTrue(control.stop_if_idle(binding,cid,state['boot']))
                code = subprocess.check_output([*docker,'wait',cid],text=True,timeout=8).strip()
                self.assertEqual(code,'0')
            finally:
                subprocess.run([*docker,'rm','-f','-v',cid],capture_output=True,check=True)
