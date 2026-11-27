import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import worker
import install

DRIVER = """
import json, os, sys
from pathlib import Path
root = Path.cwd()
state = Path(os.environ["DSH_UPDATER_STATE"])
assert Path(os.environ["DSH_UPDATER_LOG"]).parent == state
action, *args = sys.argv[1:]
with (state / "calls").open("a") as out:
    out.write(json.dumps([action, *args]) + "\\n")
if action == "current" and (root / "current").exists():
    print((root / "current").read_text())
if action == "build":
    root_source = Path(args[1])
    assert (root_source / "README.md").read_text() == "v2"
    assert not (root_source.stat().st_mode & 0o222)
    assert not ((root_source / "README.md").stat().st_mode & 0o222)
if (root / "fail").exists():
    op, code = (root / "fail").read_text().split()
    if op == action:
        print("private repository details", file=sys.stderr)
        sys.exit(int(code))
if action == "deploy":
    (root / "current").write_text(args[0])
"""


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.deployment = self.base / "deployment"
        self.data = self.deployment / "data"
        self.data.mkdir(parents=True)
        self.state = self.base / "state"
        self.driver = self.base / "deploy.py"
        self.driver.write_text(DRIVER)
        self.repo = self.base / "upstream"
        self.repo.mkdir()
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=self.repo, text=True).strip()
        git("init", "-q", "--template=", "-b", "release")
        for version in ("v1", "v2"):
            (self.repo / "README.md").write_text(version)
            git("add", "README.md")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", version)
            setattr(self, version, git("rev-parse", "HEAD"))
        (self.deployment / "current").write_text(self.v1)
        self.time = dt.datetime(2026, 1, 25, 3, 30)
        self.mailbox = self.data / worker.MAILBOX

    def open(self):
        return worker.Worker(self.deployment, self.state, self.driver,
                             source=worker.Source(self.repo.as_uri(), "file"),
                             clock=lambda: self.time.timestamp())

    def submit(self, action, request_id=None):
        item = {"id": request_id or str(uuid.uuid4()), "action": action}
        (self.mailbox / "request.json").write_text(json.dumps(item))
        return item["id"]

    def status(self):
        return json.loads((self.mailbox / "status.json").read_text())

    def calls(self):
        return [json.loads(line)[0] for line in (self.state / "calls").read_text().splitlines()]

    def test_manual_check_pinned_upgrade_and_idempotent_replay(self):
        with self.open() as host:
            host.start()
            self.submit("check")
            host.tick()
            self.assertEqual(self.status()["latest"], self.v2)
            self.assertEqual(self.status()["current"], self.v1)
            self.assertTrue(self.status()["available"])
            request_id = self.submit("upgrade")
            host.tick()
            self.assertEqual(self.status()["current"], self.v2)
            self.assertEqual(self.status()["phase"], "succeeded")
            self.assertFalse(self.status()["available"])
            self.assertFalse((self.mailbox / "request.json").exists())
            self.assertEqual(self.calls(), ["recover", "current", "current", "build", "validate", "deploy", "current"])
            self.submit("upgrade", request_id)
            host.tick()
            self.submit("upgrade")
            host.tick()
            self.assertEqual(self.calls().count("deploy"), 1)
            self.assertFalse(any(self.state.glob("source-*")))

    def test_durable_interrupted_job_never_reapplied(self):
        with self.open() as host:
            host.start()
            request_id = self.submit("upgrade")
            job = worker.snapshot(request_id, "upgrade", "manual")
            host.phase(job, None, "deploying", "interruption")
        with self.open() as host:
            host.start()
            host.tick()
            self.assertEqual(self.status()["phase"], "failed")
            self.assertFalse((self.mailbox / "request.json").exists())
            self.assertEqual(self.calls(), ["recover", "recover"])

    def test_install_after_hours_missed_window_and_daily_busy_retry(self):
        self.time = self.time.replace(hour=22)
        with self.open() as host:
            host.start()
            host.tick()
            self.assertEqual(self.calls(), ["recover"])
        self.time += dt.timedelta(days=1)
        with self.open() as host:
            host.start()
            (self.deployment / "fail").write_text("deploy 75")
            host.tick()
            self.assertEqual(self.status()["source"], "scheduled")
            self.assertEqual(self.status()["phase"], "blocked")
            count = len(self.calls())
            host.tick()
            self.assertEqual(len(self.calls()), count)
            (self.deployment / "fail").unlink()
            self.time += dt.timedelta(seconds=worker.BUSY_RETRY_SECONDS)
            host.tick()
            self.assertEqual(self.status()["phase"], "succeeded")
            count = len(self.calls())
            host.tick()
            self.assertEqual(len(self.calls()), count)
        with self.open() as host:
            host.start()
            host.tick()
            self.assertEqual(len(self.calls()), count + 1)

    def test_failure_redaction_and_no_scheduled_retry_after_revert(self):
        with self.open() as host:
            host.start()
            (self.deployment / "fail").write_text("validate 3")
            task_id = self.submit("upgrade")
            host.tick()
            self.assertNotIn("deploy", self.calls())
            self.assertEqual(self.status()["phase"], "failed")
            self.assertNotIn("private repository", json.dumps(self.status()))
            self.assertIn("private repository", (self.state / (task_id + ".log")).read_text())
            self.time = self.time.replace(hour=5)
            (self.deployment / "fail").write_text("deploy 78")
            host.tick()
            self.assertEqual(self.status()["phase"], "rolled-back")
            self.time += dt.timedelta(hours=2)
            host.tick()
            self.assertEqual(self.calls().count("deploy"), 1)

    def test_requests_and_shared_status_are_untrusted(self):
        with self.open() as host:
            host.start()
            external = self.base / "private"
            external.write_text("private")
            (self.mailbox / "request.json").symlink_to(external)
            host.tick()
            self.assertEqual(external.read_text(), "private")
            req = self.mailbox / "request.json"
            req.write_text(json.dumps({"id": str(uuid.uuid4()), "action": "upgrade", "arg": "/"}))
            host.tick()
            self.submit("reboot")
            host.tick()
            self.assertEqual(self.calls(), ["recover"])
            rid = self.submit("check")
            stage = self.mailbox / ".stage"
            os.link(req, stage)
            host.tick()
            self.assertTrue(req.exists())
            stage.unlink()
            public = self.mailbox / "status.json"
            public.unlink()
            public.symlink_to(external)
            host.tick()
            self.assertEqual(self.status()["requestId"], rid)
            self.assertEqual(external.read_text(), "private")
            self.assertFalse(public.is_symlink())

    def test_private_paths_and_lock(self):
        with self.open() as host:
            with self.assertRaises(BlockingIOError):
                with self.open():
                    self.fail()
            with self.assertRaises(ValueError):
                worker.trusted_paths(self.deployment, self.data / "bad", self.driver)
        self.mailbox.rmdir()
        self.mailbox.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(OSError):
            with self.open():
                self.fail()

    def test_explicit_compose_mount_outside_project(self):
        mounted = self.base / "user-data"
        mounted.mkdir()
        with worker.Worker(self.deployment, self.state, self.driver, data_directory=mounted) as service:
            service.mail.write("status.json", {"schema": 1})
        self.assertTrue((mounted / worker.MAILBOX / "status.json").is_file())
        self.assertFalse(self.mailbox.exists())
        with self.assertRaises(ValueError):
            worker.trusted_paths(self.deployment, mounted / "private", self.driver, mounted)
        alias = self.base / "alias"
        alias.symlink_to(mounted, target_is_directory=True)
        with self.assertRaises(ValueError):
            worker.trusted_paths(self.deployment, self.state, self.driver, alias)

    def test_uses_selected_commit_if_default_branch_moves_before_checkout(self):
        current_head = self.v2
        class MovingSource(worker.Source):
            def checkout(inner, sha, directory, log):
                (self.repo / "README.md").write_text("v3")
                subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                                "commit", "-qam", "new-remote-head"], cwd=self.repo, check=True)
                super().checkout(sha, directory, log)
        with self.open() as host:
            host.start()
            host.source = MovingSource(self.repo.as_uri(), "file")
            self.submit("upgrade")
            host.tick()
            self.assertEqual(self.status()["current"], current_head)
            self.assertEqual(self.status()["phase"], "succeeded")

    def test_reusing_action_id_and_recovery_failure(self):
        with self.open() as host:
            host.start()
            rid = self.submit("check")
            host.tick()
            self.submit("upgrade", rid)
            host.tick()
            self.assertEqual(self.status()["phase"], "failed")
            self.assertNotIn("deploy", self.calls())
        (self.deployment / "fail").write_text("recover 1")
        with self.open() as host:
            with self.assertRaises(RuntimeError):
                host.start()
            self.assertEqual(self.status()["phase"], "blocked")
            self.assertNotIn("private repository", str(self.status()))

    def test_unknown_deploy_failure_invokes_recovery_and_quiesces_on_unresolved_state(self):
        from unittest.mock import patch
        with self.open() as host:
            host.start()
            (self.deployment / "fail").write_text("deploy 7")
            self.submit("upgrade")
            host.tick()
            self.assertEqual(self.calls()[-2:], ["deploy", "recover"])
            self.assertEqual(self.status()["phase"], "failed")
            original = host.run_driver
            def failure(operation, *args):
                if operation == "recover":
                    raise worker.CommandError(1)
                return original(operation, *args)
            self.submit("upgrade")
            with patch.object(host, "run_driver", side_effect=failure), self.assertRaises(RuntimeError):
                host.tick()
            self.assertTrue((self.mailbox / "request.json").exists())
            self.assertEqual(self.status()["phase"], "blocked")
        with self.open() as host:
            host.start()
            old = len(self.calls())
            host.tick()
            self.assertEqual(len(self.calls()), old)
            self.assertFalse((self.mailbox / "request.json").exists())

    def test_cached_objects_survive_worker_restart_and_subsequent_fetch_negotiates_prior_tip(self):
        source = worker.Source(self.repo.as_uri(), "file")
        source.env["GIT_TRACE_PACKET"] = "1"
        with self.open() as host:
            host.start()
            logpath = self.state / "fetch.log"
            with logpath.open("ab") as log, tempfile.TemporaryDirectory(dir=self.state) as work:
                source.checkout(self.v1, work, log)
            initial_trace = logpath.read_text()
            self.assertIn("fetch>", initial_trace)
        with self.open() as host:
            host.start()
            with logpath.open("ab") as log, tempfile.TemporaryDirectory(dir=self.state) as work:
                source.checkout(self.v2, work, log)
                self.assertEqual((Path(work) / "README.md").read_text(), "v2")
            second_fetch = logpath.read_text()[len(initial_trace):]
            self.assertIn("have " + self.v1, second_fetch)
            from unittest.mock import patch
            with patch.object(source, "git", wraps=source.git) as git:
                with logpath.open("ab") as log, tempfile.TemporaryDirectory(dir=self.state) as work:
                    self.assertEqual(source.latest(self.state, log), self.v2)
                    source.checkout(self.v2, work, log)
                actions = [c.args[0][0] for c in git.call_args_list]
            self.assertIn("ls-remote", actions)
            self.assertNotIn("fetch", actions)
            self.assertFalse((self.state / "git-cache" / "config").exists())

    def test_scheduled_network_and_docker_unavailability_are_retried_after_ten_minutes(self):
        from unittest.mock import patch
        with self.open() as host:
            host.start()
            self.time = self.time.replace(hour=5)
            (self.deployment / "fail").write_text("current 69")
            host.tick()
            self.assertEqual(self.status()["phase"], "blocked")
            self.assertIn("Docker", self.status()["message"])
            def next_time():
                return dt.datetime.fromisoformat(json.loads((self.mailbox / "worker.json").read_text())["nextCheckAt"])
            self.assertEqual(next_time().timestamp(), self.time.timestamp() + worker.BUSY_RETRY_SECONDS)
            (self.deployment / "fail").unlink()
            self.time += dt.timedelta(seconds=worker.BUSY_RETRY_SECONDS)
            execute = worker.execute
            def dns_error(args, cwd, log, **kwargs):
                if args[:2] == ["git", "ls-remote"]:
                    log.write(b"fatal: Could not resolve host: private.example.invalid")
                    log.flush()
                    raise worker.CommandError(128)
                return execute(args, cwd, log, **kwargs)
            with patch.object(worker, "execute", side_effect=dns_error):
                host.tick()
            self.assertEqual(self.status()["phase"], "blocked")
            self.assertNotIn("private.", self.status()["message"])
            self.assertEqual(next_time().timestamp(), self.time.timestamp() + worker.BUSY_RETRY_SECONDS)
            self.time += dt.timedelta(seconds=worker.BUSY_RETRY_SECONDS)
            host.tick()
            self.assertEqual(self.status()["phase"], "succeeded")
            self.assertEqual(self.status()["current"], self.v2)

    def test_fetch_timeout_retries_but_build_failure_is_terminal(self):
        from unittest.mock import patch
        with self.open() as host:
            host.start()
            self.time = self.time.replace(hour=5)
            execute = worker.execute
            def timeout(args, cwd, log, **kwargs):
                if args[:2] == ["git", "fetch"]:
                    self.assertGreaterEqual(kwargs["timeout"], 300 if "--filter=blob:none" in args else 2700)
                    raise subprocess.TimeoutExpired(args, kwargs["timeout"])
                return execute(args, cwd, log, **kwargs)
            with patch.object(worker, "execute", side_effect=timeout):
                host.tick()
            self.assertEqual(self.status()["phase"], "blocked")
            self.assertNotIn("build", self.calls())
            self.time += dt.timedelta(seconds=worker.BUSY_RETRY_SECONDS)
            (self.deployment / "fail").write_text("build 69")
            host.tick()
            self.assertEqual(self.status()["phase"], "failed")
            self.time += dt.timedelta(seconds=worker.BUSY_RETRY_SECONDS)
            host.tick()
            self.assertEqual(self.calls().count("build"), 1)

    def test_interrupted_full_fetch_uses_bounded_batches_then_resumes_only_missing_objects(self):
        from unittest.mock import patch
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=self.repo, text=True).strip()
        git("config", "uploadpack.allowFilter", "true")
        git("config", "uploadpack.allowAnySHA1InWant", "true")
        for n in range(8):
            (self.repo / ("blob-%d" % n)).write_text("separate blob %d" % n)
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null", "commit", "-qm", "separate objects")
        selected = git("rev-parse", "HEAD")
        completed = set()
        skeletons = []
        full_fetches = []
        batches = []
        scenario = ["outage"]
        class Remote(worker.Source):
            def git(inner, args, cwd, log, **kwargs):
                if args[0] == "fetch":
                    requested = args[args.index(inner.url) + 1:]
                    if "--filter=blob:none" in args:
                        skeletons.append(True)
                    elif "--depth=1" in args:
                        full_fetches.append(True)
                        raise worker.TemporarilyUnavailable("early EOF", interrupted=True)
                    else:
                        batches.append(requested)
                        if scenario[0] == "outage" and completed:
                            raise worker.TemporarilyUnavailable("server unavailable")
                        if scenario[0] == "split" and len(requested) > 1:
                            raise worker.TemporarilyUnavailable("early EOF", interrupted=True)
                        value = super().git(args, cwd, log, **kwargs)
                        completed.update(requested)
                        return value
                return super().git(args, cwd, log, **kwargs)
        with self.open(), patch.object(worker, "BATCH_SIZE", 2), patch.object(worker, "TRANSFER_WORKERS", 1), patch.object(worker, "TRANSFER_ATTEMPTS", (0, 0)):
            remote = Remote(self.repo.as_uri(), "file")
            with (self.state / "objects.log").open("ab") as log:
                with tempfile.TemporaryDirectory(dir=self.state) as work, self.assertRaises(worker.TemporarilyUnavailable):
                    remote.checkout(selected, work, log)
                self.assertEqual(len(completed), 2)
                previous = completed.copy()
                batches.clear()
                self.assertFalse((self.state / "git-cache/tip.json").exists())
                scenario[0] = "split"
                with tempfile.TemporaryDirectory(dir=self.state) as work:
                    remote.checkout(selected, work, log)
                    self.assertEqual(sorted(p.name for p in Path(work).glob("blob-*")),
                                     ["blob-%d" % n for n in range(8)])
                    self.assertEqual(remote.missing(selected, Path(work), log), [])
                self.assertEqual(len(full_fetches), 1)
                self.assertEqual(len(skeletons), 1)
                self.assertTrue(all(0 < len(batch) <= 2 for batch in batches))
                self.assertTrue(any(len(batch) == 1 for batch in batches))
                self.assertFalse(any(previous.intersection(batch) for batch in batches))
                self.assertFalse((self.state / "git-cache/partial").exists())

    def test_copy_and_plist(self):
        source = self.base / "host"
        source.mkdir()
        (source / "worker.py").touch()
        (source / "deploy.py").touch()
        installed = self.base / "release"
        install.copy_host(source, installed)
        import plistlib
        config = plistlib.loads(install.plist(installed, self.deployment, self.state, "05:15"))
        self.assertEqual(config["ProgramArguments"][-1], "05:15")
        self.assertIn(str(installed / "worker.py"), config["ProgramArguments"])
        self.assertTrue(config["KeepAlive"])
        (source / "link").symlink_to(self.data)
        with self.assertRaises(ValueError):
            install.copy_host(source, self.base / "other")
        with self.assertRaises(Exception):
            worker.schedule_time("25:01")


if __name__ == "__main__":
    unittest.main()
