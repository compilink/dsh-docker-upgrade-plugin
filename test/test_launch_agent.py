import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
import install


class LaunchAgentTests(unittest.TestCase):
    def test_wait_for_previous_label_after_bootout(self):
        attempts = [subprocess.CompletedProcess([],5),subprocess.CompletedProcess([],0)]
        with patch.object(install.subprocess, 'run', side_effect=attempts) as run, patch.object(install.time,'sleep') as sleep:
            install.bootstrap('gui/123',Path('updater.plist'))
            self.assertEqual(run.call_count,2)
            sleep.assert_called_once()

    def test_report_permanent_bootstrap_error(self):
        with patch.object(install.subprocess,'run',return_value=subprocess.CompletedProcess([],113)), patch.object(install.time,'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'113'):
                install.bootstrap('gui/123',Path('updater.plist'))
            sleep.assert_not_called()


class NativeLaunchAgentDrill(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin' and __import__('os').environ.get('DSH_TEST_MAC_LAUNCHD') == '1',
                         'opt-in macOS user-domain LaunchAgent drill')
    def test_isolated_registration_starts_fixture_and_unloads_only_its_test_label(self):
        import os
        import tempfile
        import time
        import uuid

        self.assertNotEqual(os.getuid(), 0)
        with tempfile.TemporaryDirectory(prefix='dsh-onboard-', dir='/private/tmp') as temporary:
            root = Path(temporary)
            data = root / 'mock-data'
            data.mkdir()
            app = root / 'private-host'
            directory = root / 'agents'
            label_prefix = 'io.github.compilink.dsh-onboard-test.' + uuid.uuid4().hex
            binding = dict(composeFile=str(root / 'compose.yaml'), project='fixture', service='fixture',
                           projectDirectory=str(root), dataDirectory=str(data))
            label = label_prefix + '.' + install.binding_id(binding)
            job = f'gui/{os.getuid()}/{label}'

            def copy_fixture(_source, destination):
                destination.mkdir()
                (destination / 'portable_deploy.py').write_text('raise SystemExit("fixture only")\n')
                (destination / 'worker.py').write_text(
                    'import sys, time\nfrom pathlib import Path\n'
                    'state = Path(sys.argv[sys.argv.index("--state") + 1])\n'
                    'data = Path(sys.argv[sys.argv.index("--data-directory") + 1])\n'
                    'assert state.parents[3] == data.parent and data.name == "mock-data"\n'
                    '(state / "fixture-started").write_text("ready")\ntime.sleep(60)\n')

            with patch.object(install, 'APP', app), patch.object(install, 'LABEL', label_prefix), \
                    patch.object(install, 'agents_directory', return_value=directory), \
                    patch.object(install, 'copy_portable', side_effect=copy_fixture):
                try:
                    self.assertEqual(install.register_portable(binding, '15:31'), '15:31')
                    state = app / 'instances' / install.binding_id(binding) / 'state'
                    deadline = time.monotonic() + 20
                    while not (state / 'fixture-started').exists() and time.monotonic() < deadline:
                        time.sleep(0.1)
                    self.assertTrue((state / 'fixture-started').exists(), 'fixture worker did not acknowledge its binding')
                    self.assertEqual((state / 'fixture-started').read_text(), 'ready')
                    self.assertEqual(install.register_portable(binding, '04:00'), '15:31')
                    self.assertEqual(list(data.iterdir()), [])
                finally:
                    subprocess.run([install.LAUNCHCTL, 'bootout', job], capture_output=True)
                    deadline = time.monotonic() + 15
                    while subprocess.run([install.LAUNCHCTL, 'print', job], capture_output=True).returncode == 0:
                        if time.monotonic() >= deadline:
                            self.fail('fixture-only LaunchAgent still registered')
                        time.sleep(0.1)
