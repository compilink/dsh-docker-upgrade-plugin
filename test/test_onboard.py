import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
import install
import onboard


class OnboardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.data = self.root / 'persisted state'
        module = self.data / 'profiles/web/node_modules' / onboard.BUNDLE
        module.mkdir(parents=True)
        (module / 'package.json').write_text(json.dumps(dict(name=onboard.BUNDLE)))
        self.compose = self.root / 'compose.yml'
        self.compose.write_text('services: {}')
        self.compose.chmod(0o600)
        self.info = dict(running=True, mounts=[dict(Type='bind', Source=str(self.data), Destination='/data', RW=True)],
                         ports={'80/tcp': [dict(HostIp='127.0.0.1', HostPort='5080')]}, labels={
                             'com.docker.compose.project': 'example', 'com.docker.compose.service': 'web',
                             'com.docker.compose.project.working_dir': str(self.root),
                             'com.docker.compose.project.config_files': str(self.compose),'com.docker.compose.config-hash':'e'*64},
                         name='/example-web-1', id='a'*64, image='sha256:'+'b'*64)
        self.binding = self.candidate()
        self.app = self.root / 'Host service'
        self.agents = self.root / 'LaunchAgents'
        self.addCleanup(patch.stopall)
        patch.object(install, 'APP', self.app).start()
        patch.object(install, 'agents_directory', return_value=self.agents).start()

    def candidate(self):
        return onboard.candidate(self.info, onboard.DOCKER, 'desktop-linux', 'unix:///var/run/docker.sock')

    def test_discovers_arbitrary_port_and_data_directory(self):
        self.assertEqual(self.binding['dataDirectory'], str(self.data))
        self.assertEqual(self.binding['hostPort'], 5080)
        self.assertEqual(self.binding['containerPort'], 80)

    def test_remote_context_refused_before_daemon(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(onboard, 'run', side_effect=[
                'elsewhere', 'ssh://someone@remote']) as run:
            with self.assertRaisesRegex(ValueError, 'Unix'):
                onboard.local_context()
            self.assertEqual(run.call_count, 2)
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://remote:2375'}), patch.object(onboard, 'run') as run:
            with self.assertRaisesRegex(ValueError, '默认环境'):
                onboard.local_context()
            run.assert_not_called()

    def test_config_inside_data_untrusted_and_named_volumes_refused(self):
        config = self.data / 'compose.yaml'
        config.write_text('services: {}')
        self.info['labels']['com.docker.compose.project.config_files'] = str(config)
        with self.assertRaisesRegex(ValueError, 'Compose 文件'):
            self.candidate()
        self.info['mounts'][0]['Type'] = 'volume'
        with self.assertRaisesRegex(ValueError, '命名卷'):
            self.candidate()

    def test_unrelated_profiles_and_unsafe_port_bindings(self):
        manifest = self.data / 'profiles/web/node_modules' / onboard.BUNDLE / 'package.json'
        manifest.write_text('{"name":"not-this-plugin"}')
        self.assertIsNone(self.candidate())
        manifest.write_text(json.dumps(dict(name=onboard.BUNDLE)))
        self.info['ports']['80/tcp'][0]['HostIp'] = '192.0.2.10'
        with self.assertRaisesRegex(ValueError, '回环'):
            self.candidate()

    def test_choose_targets_and_cancel(self):
        second = dict(self.binding, container='second', hostPort=5444)
        with patch.object(onboard, 'native', return_value=onboard.name_for(second)):
            self.assertEqual(onboard.select([self.binding, second], []), second)
        with patch.object(onboard.subprocess, 'run', return_value=subprocess.CompletedProcess([],1,'','(-128)')):
            with self.assertRaises(onboard.Cancelled):
                onboard.native(['return 1'])
        with self.assertRaisesRegex(ValueError, '命名卷'):
            onboard.select([], ['不支持命名卷'])

    def install_legacy_plist(self, schedule):
        host = self.app / 'releases/host-old/host'
        host.mkdir(parents=True)
        (host / 'deploy.py').touch()
        self.agents.mkdir()
        data = self.root / 'data'
        data.mkdir()
        self.binding['dataDirectory'] = str(data)
        self.path = self.agents / (install.LABEL + '.plist')
        self.path.write_bytes(install.plist(host, self.root, self.app / 'state', schedule))

    def test_existing_legacy_preserves_plan_without_preflight_or_registration(self):
        self.install_legacy_plist('03:12')
        old = self.path.read_bytes()
        with patch.object(install.subprocess, 'run', return_value=subprocess.CompletedProcess([],0)), \
                patch.object(onboard, 'discover', return_value=([self.binding], [])), \
                patch.object(onboard,'preflight') as preflight, patch.object(install, 'register_portable') as register, \
                patch.object(onboard,'note') as note:
            onboard.onboard()
            self.assertIn('03:12', note.call_args.args[0])
            register.assert_not_called()
            preflight.assert_not_called()
        self.assertEqual(old, self.path.read_bytes())

    def test_preflight_failure_cancellation_and_target_drift_never_register(self):
        with patch.object(install, 'register_portable') as register, \
                patch.object(onboard, 'discover', return_value=([self.binding], [])) as discovery:
            with patch.object(onboard, 'preflight', side_effect=ValueError('unsupported')):
                with self.assertRaisesRegex(ValueError, 'unsupported'):
                    onboard.onboard()
            with patch.object(onboard, 'preflight'), patch.object(onboard, 'native', side_effect=onboard.Cancelled):
                with self.assertRaises(onboard.Cancelled):
                    onboard.onboard()
            discovery.side_effect = [([self.binding], []), ([dict(self.binding, containerId='f'*64)], [])]
            with patch.object(onboard, 'preflight'), patch.object(onboard, 'native', return_value='03:00'):
                with self.assertRaisesRegex(ValueError, '发生变化'):
                    onboard.onboard()
            register.assert_not_called()

    def test_registers_one_private_binding_idempotently_and_rejects_shared_data(self):
        def copy(_source, dest):
            dest.mkdir()
            (dest / 'portable_deploy.py').touch()
            (dest / 'worker.py').touch()
        with patch.object(install, 'copy_portable', side_effect=copy), patch.object(install, 'bootstrap') as bootstrap, \
                patch.object(install.subprocess, 'run', return_value=subprocess.CompletedProcess([],113)) as launch:
            self.assertEqual(install.register_portable(self.binding, '02:31'), '02:31')
            agent, = self.agents.glob('*.plist')
            args = plistlib.loads(agent.read_bytes())['ProgramArguments']
            self.assertEqual(args[args.index('--data-directory')+1], str(self.data))
            state = Path(args[args.index('--state')+1])
            self.assertNotIn(self.data, state.parents)
            self.assertEqual(json.loads((state/'binding.json').read_text()), self.binding)
            self.assertEqual((state/'binding.json').stat().st_mode & 0o777, 0o600)
            launch.return_value = subprocess.CompletedProcess([],0)
            self.assertEqual(install.register_portable(dict(self.binding, containerId='c'*64), '05:12'), '02:31')
            with self.assertRaisesRegex(ValueError, '已绑定'):
                install.register_portable(dict(self.binding, project='duplicate'), '07:00')
            different = self.root / "different data"
            different.mkdir()
            with self.assertRaisesRegex(ValueError, '其他数据目录'):
                install.register_portable(dict(self.binding, dataDirectory=str(different)), '07:00')
            bootstrap.assert_called_once()

    def test_failed_bootstrap_leaves_no_launch_agent_or_binding(self):
        def copy(_source, dest):
            dest.mkdir()
        with patch.object(install, 'copy_portable', side_effect=copy), \
                patch.object(install.subprocess, 'run', return_value=subprocess.CompletedProcess([],113)), \
                patch.object(install, 'bootstrap', side_effect=RuntimeError("bootstrap failed")):
            with self.assertRaisesRegex(RuntimeError, 'bootstrap'):
                install.register_portable(self.binding, '02:31')
        self.assertEqual(list(self.agents.glob('*.plist')), [])
        self.assertEqual(list(self.app.glob('instances/*/state/binding.json')), [])

    def test_diagnostic_works_with_standard_profile_without_plugin(self):
        profile = self.data / 'profiles/web'
        (profile / 'package.json').write_text('{"name":"dsh-profile-web","private":true}')
        (profile / 'node_modules' / onboard.BUNDLE / 'package.json').unlink()
        self.assertEqual(self.candidate(), self.binding)
        fake_image = {'registryDigest': True, 'declaresOfficialSource': False, 'hasSourceRevision': True, 'declaredRevision': 'a' * 40,
                      'registry': 'example.corp.invalid', 'privateToken': 'EXAMPLE_NEVER_COPY'}
        with patch.object(onboard, 'discover', return_value=([self.binding], [])), \
                patch.object(onboard, 'driver_check', return_value=fake_image) as check, \
                patch.object(onboard, 'native') as native:
            report = onboard.diagnostic_summary()
            self.assertTrue(report['instances'][0]['hasSourceRevision'])
            self.assertNotIn('a' * 40, json.dumps(report))
            self.assertNotIn('EXAMPLE_NEVER_COPY', json.dumps(report))
            self.assertNotIn('corp.invalid', json.dumps(report))
            self.assertNotIn(str(self.data), json.dumps(report))
            onboard.diagnostic_dialog(report)
            script, message = native.call_args.args
            self.assertTrue(any('button returned' in line and 'clipboard' in line for line in script))
            self.assertNotIn(str(self.root), message)
            self.assertNotIn(self.binding['container'], message)
            check.assert_called_once_with(self.binding, 'diagnose')

    def test_image_allowlist_discards_tags_and_supports_other_mount_layouts_for_diagnostics(self):
        repo = 'ghcr.io/obrige/dsh-docker'
        self.info.update(ref=repo+':private-tag@sha256:'+'f'*64)
        self.info['mounts'] = [dict(Destination='/unlisted/private-path', Type='volume')]
        result = onboard.public_image_info(self.info)
        self.assertEqual(result, dict(publicImage=repo, compose='single-file', data='unconfirmed', mountTypes=['volume']))
        for reference in ('corp.invalid/dsh', 'ghcr.io/obrige/dsh-docker-private:tag',
                          'ghcr.io/obrige/dsh-docker/secret:latest'):
            self.assertIsNone(onboard.public_image_info(dict(self.info, ref=reference)))
        def discovery(*, known_images):
            known_images['a'*64] = result
            return [], []
        with patch.object(onboard, 'discover', side_effect=discovery), patch.object(onboard, 'native') as native:
            report = onboard.diagnostic_summary()
            self.assertEqual(report['detected'], 0)
            self.assertEqual(report['otherPublicImages'], [result])
            onboard.diagnostic_dialog(report)
            text = native.call_args.args[1]
            self.assertIn(repo, text)
            self.assertNotIn('private', text)
            self.assertNotIn(self.info['id'], text)

    def test_npm_app_bind_and_workspace_volume_with_ssh_and_web_ports(self):
        self.info.update(npmVersion="0.2.2-rc.5", ref="fixture-app:local", launch=[["dsh-entrypoint"], ["web"]])
        self.info['mounts'][0]['Destination'] = '/app/data'
        self.info['mounts'].append(dict(Type='volume', Destination='/workspace', Name='private_volume', RW=True))
        self.info['ports'] = {'22/tcp': [dict(HostIp='0.0.0.0', HostPort='2022')],
                              '3080/tcp': [dict(HostIp='127.0.0.1', HostPort='9930')]}
        (self.data / 'profiles/web/node_modules' / onboard.BUNDLE / 'package.json').unlink()
        model = self.model({'type':'bind','source':str(self.data),'target':'/app/data'},
                           {'type':'volume','source':'work','target':'/workspace'})
        with patch.object(onboard,'run',side_effect=self.compose_run(model)) as run:
            bound = self.candidate()
        self.assertIn('--no-env-resolution', run.call_args_list[0].args[0])
        self.assertRegex(bound['composeConfigSHA256'], r'^[a-f0-9]{64}$')
        self.assertEqual(bound['storage']['workspace']['source'], 'private_volume')
        self.assertEqual((bound['containerDataPath'], bound['dataDirectory']), ('/app/data', str(self.data)))
        self.assertEqual((bound['containerPort'], bound['hostPort']), (3080, 9930))
        self.assertEqual(bound['workspaceMountType'], 'volume')
        self.assertTrue(bound['sshPublished'])
        self.assertTrue(bound['sshExposed'])
        self.assertEqual(bound['npmVersion'], '0.2.2-rc.5')
        del self.info['ports']['3080/tcp']
        with self.assertRaisesRegex(ValueError, 'SSH'):
            self.candidate()

    def test_npm_volume_is_recognized_without_constructing_host_data_directory(self):
        self.info.update(npmVersion='1.0.0', ref='private.invalid/company-image:secret',
                         mounts=[dict(Destination='/app/data', Type='volume', Name='private-dsh-volume', RW=True),
                                 dict(Destination='/workspace', Type='bind', Source=str(self.data), RW=True)],
                         exposed={'22/tcp': {}, '3080/tcp': {}}, ports={'3080/tcp': [dict(HostIp='192.0.2.10', HostPort='3080')]})
        with self.assertRaisesRegex(ValueError, '回环'):
            self.candidate()
        summary = onboard.public_image_info(self.info)
        self.assertEqual(summary['data'], 'volume')
        self.assertEqual(summary['workspaceMountType'], 'bind')
        self.assertEqual(summary['web'], 'specific-interface')
        self.assertTrue(summary['sshExposed'])
        self.assertFalse(summary['sshPublished'])
        self.assertNotIn('private', json.dumps(summary))
        self.assertNotIn('publicImage', summary)
        with patch.object(onboard, 'native') as native:
            onboard.diagnostic_dialog({'detected':0,'instances':[],'otherPublicImages':[summary],'notes':[]})
            message = native.call_args.args[1]
            self.assertIn('/app/data：Docker 卷', message)
            self.assertIn('/workspace：宿主目录', message)
            self.assertIn('已声明未映射', message)
            self.assertNotIn('private', message)

    def test_static_manifest_accepts_only_fixed_name_regular_single_file_and_bounded_version(self):
        import io
        import tarfile
        def archive(name='package.json', manifest=None, kind=tarfile.REGTYPE, extra=False):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode='w:') as tar:
                payload = json.dumps({'name':'@deepseek-ai/dsh','version':'0.2.2-rc.5'} if manifest is None else manifest).encode()
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind == tarfile.REGTYPE:
                    member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload) if member.size else None)
                if extra:
                    tar.addfile(tarfile.TarInfo('additional'))
            return raw.getvalue()
        self.assertEqual(onboard.manifest_version(archive()), '0.2.2-rc.5')
        for bad in (archive(kind=tarfile.SYMTYPE), archive('../package.json'), archive(extra=True),
                    archive(manifest={'name':'other','version':'1.0.0'}),
                    archive(manifest={'name':'@deepseek-ai/dsh','version':'1.0.0-secretlabel'}),
                    b'X' * (onboard.ARCHIVE_LIMIT + 1)):
            self.assertIsNone(onboard.manifest_version(bad))
        self.info['mounts'] = [{'Destination':'/app/data'},{'Destination':'/app/node_modules'}]
        with patch.object(onboard.subprocess, 'Popen') as popen:
            self.assertIsNone(onboard.npm_version(self.info, 'docker','desktop-linux'))
            popen.assert_not_called()

    def test_reader_uses_docker_cp_and_caps_output_without_running_container_programs(self):
        import io
        import tarfile
        self.info['mounts'] = [{'Destination':'/app/data'}]
        raw = io.BytesIO()
        contents = b'{"name":"@deepseek-ai/dsh","version":"1.0.0"}'
        with tarfile.open(fileobj=raw, mode='w:') as tar:
            member = tarfile.TarInfo('package.json')
            member.size = len(contents)
            tar.addfile(member, io.BytesIO(contents))
        fixture = self.root / 'fixture.tar'
        fixture.write_bytes(raw.getvalue())
        popen = subprocess.Popen
        calls = []
        def substitute(args, **kwargs):
            calls.append(args)
            return popen(['/bin/cat', str(fixture)], **kwargs)
        with patch.object(onboard.subprocess, 'Popen', side_effect=substitute):
            self.assertEqual(onboard.npm_version(self.info, onboard.DOCKER, 'desktop-linux'), '1.0.0')
            fixture.write_bytes(b'b' * (onboard.ARCHIVE_LIMIT + 20000))
            self.assertIsNone(onboard.npm_version(self.info, onboard.DOCKER, 'desktop-linux'))
        self.assertTrue(all(argv == [onboard.DOCKER, '--context', 'desktop-linux', 'cp',
                            self.info['id'] + ':/app/node_modules/@deepseek-ai/dsh/package.json', '-'] for argv in calls))

    @staticmethod
    def compose_run(model):
        return lambda command, **kw: 'web ' + 'e'*64 if '--hash=web' in command else json.dumps(model)

    def model(self, data, workspace):
        return {"name":"example","services":{"web":{"image":self.info.get('ref','fixture-app:local'),
                "ports":[{"target":3080,"published":"9930","host_ip":"127.0.0.1","protocol":"tcp"}],
                "volumes":[data,workspace], "environment":{"EXAMPLE":"kept-out-of-reports"}}},
                "volumes":{"work":{"name":"private_volume"},"store":{"name":"example_data"}}}

    def test_normalized_named_and_bound_mounts_match_daemon_or_refuse(self):
        self.info.update(npmVersion='1.0.0',ref="fixture-app:local",launch=[None,['web']],
                         mounts=[dict(Destination='/app/data',Type='volume',Name='example_data',RW=True),
                                 dict(Destination='/workspace',Type='bind',Source=str(self.data),RW=True)],
                         ports={"3080/tcp":[dict(HostIp="127.0.0.1", HostPort="9930")]})
        model = self.model(dict(type="volume",source="store",target="/app/data"),
                           dict(type="bind",source=str(self.data), target="/workspace"))
        with patch.object(onboard,'run',side_effect=self.compose_run(model)):
            first = self.candidate()
            self.assertNotIn('dataDirectory',first)
            self.assertEqual(first['storage']['data'],dict(type="volume",source="example_data",target="/app/data"))
            self.info['ref'] = model['services']['web']['image'] = 'candidate:approved'
            self.assertEqual(first['composeConfigSHA256'],self.candidate()['composeConfigSHA256'])
            model['services']['web']['environment']['EXAMPLE'] = "independent operator change"
            self.assertNotEqual(first['composeConfigSHA256'],self.candidate()['composeConfigSHA256'])
            model['volumes']['store']['name'] = 'other'
            with self.assertRaisesRegex(onboard.SetupError,'挂载与正在运行'):
                self.candidate()
            model['services']['web']['volumes'][0].pop('source')
            with self.assertRaisesRegex(onboard.SetupError,'自动分配'):
                self.candidate()
        self.assertNotIn('kept-out-of-reports', json.dumps(first))

    def test_relocated_compose_file_requires_native_choice_and_matching_container_hash(self):
        self.info.update(npmVersion='1.0.0', ref="fixture-app:local",
                         mounts=[dict(Destination='/app/data',Type='volume',Name='example_data',RW=True),
                                 dict(Destination='/workspace',Type='bind',Source=str(self.data),RW=True)],
                         ports={"3080/tcp":[dict(HostIp="127.0.0.1", HostPort="9930")]})
        self.info['labels']['com.docker.compose.project.config_files'] = str(self.root/'old/compose.yml')
        model = self.model(dict(type="volume",source="store",target="/app/data"),
                           dict(type="bind",source=str(self.data),target="/workspace"))
        record = self.info, onboard.DOCKER, 'desktop-linux','unix:///var/run/docker.sock'
        with self.assertRaises(onboard.MissingCompose):
            onboard.candidate(*record)
        with patch.object(onboard,'native',side_effect=onboard.Cancelled), patch.object(onboard,'run') as run:
            with self.assertRaises(onboard.Cancelled):
                onboard.select_directory(record)
            run.assert_not_called()
        with patch.object(onboard,'native',return_value=str(self.root)), patch.object(onboard,'run',side_effect=self.compose_run(model)):
            selected = onboard.select_directory(record)
            self.assertTrue(selected['composeSelected'])
            self.assertEqual(selected['composeFile'],str(self.compose))
            self.assertEqual(onboard.candidate(*record,approved_compose=selected), selected)
            self.info['labels']['com.docker.compose.config-hash'] = 'c'*64
            with self.assertRaisesRegex(onboard.SetupError,'创建这个容器'):
                onboard.candidate(*record,approved_compose=selected)

    def test_intersecting_workspace_refused_before_private_or_launchagent_writes(self):
        self.data.mkdir(exist_ok=True)
        binding = dict(self.binding, storage={
            "data": {"type":"bind","target":"/app/data","source":str(self.data)},
            "workspace":{"type":"bind","target":"/workspace","source":str(self.root)}})
        with patch.object(install, 'bootstrap') as bootstrap:
            with self.assertRaisesRegex(ValueError, 'LaunchAgents'):
                install.register_portable(binding,'02:18')
            binding['storage']['workspace']['source'] = str(self.agents/'operator-subfolder')
            with self.assertRaisesRegex(ValueError, 'LaunchAgents'):
                install.register_portable(binding,'02:18')
            bootstrap.assert_not_called()
        self.assertFalse(self.app.exists())
        self.assertFalse(self.agents.exists())

    def test_all_inputs_available_needs_only_one_native_confirmation(self):
        with patch.object(onboard,'discover',return_value=([self.binding],[])), patch.object(onboard,'preflight'), \
                patch.object(onboard,'native',return_value='04:00') as native, \
                patch.object(onboard,'note') as note, patch.object(install, 'register_portable',return_value='04:00') as register, \
                patch('builtins.print'):
            onboard.onboard()
            native.assert_called_once()
            prompt = native.call_args.args[1]
            for required in ('官方 GitHub 默认分支', '启动入口和 SSH 配置', '无需浏览器登录凭据', '04:00'):
                self.assertIn(required, prompt)
            self.assertNotIn(str(self.root), prompt)
            register.assert_called_once_with(self.binding,'04:00')
            note.assert_not_called()

    def test_wildcard_compose_publishing_is_retained_with_a_local_web_origin(self):
        self.info.update(npmVersion='0.2.2-rc.5', ref='fixture-app:local',
                         mounts=[dict(Destination='/app/data',Type='volume',Name='example_data',RW=True),
                                 dict(Destination='/workspace',Type='bind',Source=str(self.data),RW=True)],
                         ports={'3080/tcp':[dict(HostIp='0.0.0.0',HostPort='9930'),dict(HostIp='::',HostPort='9930')],
                                '22/tcp':[dict(HostIp='0.0.0.0',HostPort='7022')]})
        model = self.model(dict(type="volume",source="store",target="/app/data"),
                           dict(type="bind",source=str(self.data),target="/workspace"))
        model['services']['web']['ports'][0].pop('host_ip')
        with patch.object(onboard,'run',side_effect=self.compose_run(model)):
            value = self.candidate()
            self.assertEqual((value['hostPort'],value['loopbackHost'],value['webPublish']), (9930,'127.0.0.1','published'))
            self.assertIn('127.0.0.1:9930',onboard.name_for(value))
            self.assertTrue(value['sshPublished'])
        self.assertEqual(onboard.local_web_mapping([dict(HostIp='::1',HostPort='4000')]), (4000,'::1'))
