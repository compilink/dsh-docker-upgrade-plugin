import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import onboard
import portable_deploy as driver
import source_build


class PortableInspection(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve()
        self.binding = {
            "docker": onboard.DOCKER, "context": "desktop-linux", "endpoint": "unix:///var/run/docker.sock",
            "containerId": "1" * 64, "imageId": "sha256:" + "2" * 64,
            "dataDirectory": "/private-path-not-for-diagnostics",
        }
        self.file = self.state / "binding.json"
        self.file.write_text(json.dumps(self.binding))
        self.file.chmod(0o600)
        self.context = patch.object(onboard, "local_context", return_value=(self.binding["context"], self.binding["endpoint"]))
        self.context.start()
        self.addCleanup(self.context.stop)
        self.env = patch.dict(os.environ, {"DSH_UPDATER_STATE": str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, operation):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = driver.main([operation])
        return code, out.getvalue(), err.getvalue()

    def test_only_reads_selected_container_and_image_and_removes_untrusted_labels(self):
        image = dict(id=self.binding["imageId"], revision="secret-on-bad-label", registryDigest=True, upstream=False)
        with patch.object(onboard, "candidate", return_value=self.binding), patch.object(
                onboard, "run", side_effect=["{}", json.dumps(image)]) as run:
            code, out, err = self.invoke("diagnose")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {
            "compose": "single-file", "data": "bind", "web": "loopback", "hasSourceRevision": False,
            "declaresOfficialSource": False, "registryDigest": True,
        })
        self.assertEqual([call.args[0][3] for call in run.call_args_list], ["inspect", "image"])
        self.assertNotIn("secret", out)
        self.assertNotIn(self.binding["dataDirectory"], out)

    def test_labels_cannot_authorize_a_build_or_registration(self):
        image = dict(id=self.binding["imageId"], revision="a" * 40, registryDigest=True, upstream=True)
        with patch.object(onboard, "candidate", return_value=self.binding), patch.object(
                onboard, "run", side_effect=["{}", json.dumps(image)]):
            code, out, err = self.invoke("preflight")
        self.assertEqual((code, out), (78, ""))
        self.assertIn("npm 安装布局", err)

    def test_binding_must_be_private_not_a_link_or_an_external_binary(self):
        self.file.chmod(0o644)
        with self.assertRaises(driver.Unsupported):
            driver.read_binding(self.state)
        self.file.rename(self.state / "actual")
        self.file.symlink_to("actual")
        with self.assertRaises(OSError):
            driver.read_binding(self.state)
        self.file.unlink()
        self.file.write_text(json.dumps(dict(self.binding, docker="/bin/echo")))
        self.file.chmod(0o600)
        with self.assertRaises(driver.Unsupported):
            driver.read_binding(self.state)

    def test_changed_binding_stops_before_reading_image(self):
        with patch.object(onboard, "candidate", return_value=dict(self.binding, hostPort=8080)), patch.object(
                onboard, "run", return_value="{}") as run:
            code, _, err = self.invoke("diagnose")
        self.assertEqual(code, 78)
        self.assertIn("重新选择", err)
        self.assertEqual(run.call_count, 1)

    def test_inspection_failure_cannot_leak_private_error_strings(self):
        with patch.object(onboard, "run", side_effect=ValueError("/host/private-path --token=secret")):
            code, out, err = self.invoke("diagnose")
        self.assertEqual((code, out), (78, ""))
        self.assertNotIn("secret", err)
        self.assertNotIn("private-path", err)

    def test_malformed_source_operation_cannot_touch_docker(self):
        with patch.object(onboard, "run") as run:
            for operation in ("build", "validate", "deploy"):
                with self.subTest(operation=operation):
                    self.assertNotEqual(self.invoke(operation)[0], 0)
        run.assert_not_called()

    def test_unmarked_npm_image_has_no_known_commit_even_if_it_declares_a_git_revision(self):
        sha = "a" * 40
        binding = dict(self.binding, containerDataPath="/app/data", npmVersion="0.2.2-rc.5")
        facts = {"id": binding["imageId"], "revision": sha}
        with patch.object(onboard, "run"), patch.object(driver, "inspect_binding"), patch.object(source_build, "image_facts", return_value=facts):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(driver.dispatch("current", [], binding, self.state), 0)
            self.assertEqual(output.getvalue(), "")
        marked = dict(facts, recipe=source_build.RECIPE)
        with self.assertRaises(source_build.BuildError):
            source_build.tracked(binding, self.state, marked)
        record = {"source": sha, "root": binding["imageId"], "image": binding["imageId"]}
        source_build.write_record(source_build.paths(self.state, sha), record)
        self.assertEqual(source_build.tracked(binding, self.state, marked), record)

    def test_unavailable_docker_is_retryable_without_starting_recovery_on_an_idle_host(self):
        with patch.object(onboard, "run", side_effect=onboard.SetupError("Docker unavailable")) as run:
            self.assertEqual(driver.dispatch("recover", [], self.binding, self.state), 0)
            run.assert_not_called()
            self.assertEqual(driver.dispatch("current", [], self.binding, self.state), 69)

    def test_validation_receipt_is_written_only_after_isolated_runtime_checks(self):
        import portable_transaction
        sha = "b" * 40
        image = "sha256:" + "c" * 64
        binding = dict(self.binding, containerDataPath="/app/data", npmVersion="0.2.2-rc.5")
        record = dict(image=image, source=sha, baseline=binding["imageId"], validated=False)
        path = source_build.paths(self.state, sha)
        source_build.write_record(path,record)
        facts = dict(id=image, recipe=source_build.RECIPE, revision=sha)
        with patch.object(driver, "inspect_binding"), patch.object(source_build, "image_facts", return_value=facts), \
                patch.object(source_build, "smoke") as smoke, patch.object(portable_transaction, "validate") as validate:
            validate.side_effect = RuntimeError("candidate health check failed")
            with self.assertRaises(RuntimeError):
                driver.dispatch("validate", [sha], binding, self.state)
            self.assertFalse(json.loads(path.read_bytes())["validated"])
            with self.assertRaises(source_build.BuildError):
                source_build.candidate(binding, self.state, sha, validated=True)
            validate.side_effect = None
            self.assertEqual(driver.dispatch("validate", [sha], binding, self.state), 0)
            self.assertTrue(json.loads(path.read_bytes())["validated"])
            smoke.assert_called_with(binding, image, sha)

    def test_source_checkout_must_be_under_private_worker_state(self):
        with tempfile.TemporaryDirectory() as other, patch.object(source_build.subprocess, "run") as run:
            with self.assertRaises(source_build.BuildError):
                source_build.build(self.binding, self.state, "a" * 40, other)
        run.assert_not_called()
