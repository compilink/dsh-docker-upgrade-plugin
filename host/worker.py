#!/usr/bin/env python3
"""Run the DSH source updater as a private, single-host process."""
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

UPSTREAM = "https://github.com/deepseek-ai/deepseek-harness.git"
MAILBOX = "dsh-source-updater"
NOFOLLOW = os.O_NOFOLLOW
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW
COMMIT = re.compile(r"[0-9a-f]{40}")
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
TERMINAL = {"succeeded", "failed", "blocked", "rolled-back"}
BUSY_RETRY_SECONDS = 600
BATCH_SIZE = 128
TRANSFER_WORKERS = 4
TRANSFER_ATTEMPTS = (0, 1, 3)


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def schedule_time(value):
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise argparse.ArgumentTypeError("use HH:MM in local 24-hour time")
    return value


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise ValueError("updater state requires a private directory owned by this user")
    return path


def data_root(deployment, data_directory=None):
    path = Path(data_directory) if data_directory is not None else Path(deployment) / "data"
    if not path.is_absolute() or not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("expected an existing data directory, not a link")
    return path.resolve(strict=True)


def trusted_paths(deployment, state, driver, data_directory=None, *, docker_mailbox=False):
    deployment = Path(deployment).resolve(strict=True)
    state = Path(state).expanduser().absolute()
    driver = Path(driver).resolve(strict=True)
    if not docker_mailbox:
        data = data_root(deployment, data_directory)
        if state.resolve() in data.parents:
            raise ValueError("host state cannot contain the DSH data directory")
        for trusted in (state.resolve(), driver):
            if data in trusted.parents or trusted == data:
                raise ValueError("private state and host driver must be outside DSH data")
    info = driver.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
        raise ValueError("host driver needs a trusted owner and file permissions")
    return deployment, private_dir(state).resolve(), driver


class Mailbox:
    def __init__(self, data_directory):
        with contextlib.ExitStack() as resources:
            data = os.open(data_directory, DIR_FLAGS)
            resources.callback(os.close, data)
            try:
                os.mkdir(MAILBOX, 0o755, dir_fd=data)
            except FileExistsError:
                pass
            self.fd = os.open(MAILBOX, DIR_FLAGS, dir_fd=data)

    def close(self):
        os.close(self.fd)

    def write(self, name, value):
        temporary = "." + uuid.uuid4().hex + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(json.dumps(value, ensure_ascii=False).encode("utf8") + b"\n")
                os.fchmod(out.fileno(), 0o644)
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self.fd)

    def request(self):
        try:
            entry = os.stat("request.json", dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        identity = (entry.st_dev, entry.st_ino)
        if stat.S_ISREG(entry.st_mode) and entry.st_nlink != 1:
            # Atomic link publication briefly has two names until its staging name is removed.
            return None
        if not stat.S_ISREG(entry.st_mode) or entry.st_size > 1024:
            return (None, identity)
        try:
            fd = os.open("request.json", os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW, dir_fd=self.fd)
            with os.fdopen(fd, "rb") as request:
                info = os.fstat(request.fileno())
                if (info.st_dev, info.st_ino) != identity or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return None
                raw = request.read(1025)
            if len(raw) > 1024:
                return (None, identity)
            pairs = json.loads(raw.decode("utf-8"), object_pairs_hook=list)
            if not isinstance(pairs, list) or len(pairs) != 2 or not raw.lstrip().startswith(b"{"):
                return (None, identity)
            value = dict(pairs)
            if (set(value) == {"id", "action"} and isinstance(value["id"], str) and
                    UUID4.fullmatch(value["id"]) and value["action"] in ("check", "upgrade")):
                return (value, identity)
        except (OSError, ValueError, TypeError, RecursionError):
            pass
        return (None, identity)

    def acknowledge(self, identity):
        # The publisher keeps the inode in place until acknowledgment.
        try:
            entry = os.stat("request.json", dir_fd=self.fd, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode) and (entry.st_dev, entry.st_ino) == identity:
                os.unlink("request.json", dir_fd=self.fd)
        except FileNotFoundError:
            pass


class CommandError(Exception):
    def __init__(self, code):
        self.code = code


def execute(args, cwd, log, env=None, capture=False, timeout=3600, pass_fds=()):
    process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stderr=log,
                               stdout=subprocess.PIPE if capture else log, env=env,
                               start_new_session=True, pass_fds=pass_fds)
    try:
        out, _ = process.communicate(timeout=timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise
    if process.returncode:
        raise CommandError(process.returncode)
    return (out or b"").decode("utf-8", errors="replace").strip()


class TemporarilyUnavailable(Exception):
    """A transport or availability failure before any production change."""

    def __init__(self, message, interrupted=False):
        super().__init__(message)
        self.interrupted = interrupted


TRANSIENT_GIT = re.compile(
    rb"could not resolve (?:host|proxy)|failed to connect|couldn.t connect|connection (?:refused|reset|closed|timed out)"
    rb"|network is unreachable|operation (?:timed out|too slow)|early eof|unexpected disconnect"
    rb"|remote end hung up|ssl_error_syscall|tls connection was non-properly terminated"
    rb"|curl (?:18|28|35|52|55|56|92)\b|http (?:429|5[0-9][0-9])\b|requested url returned error:\s*(?:429|5[0-9][0-9])\b", re.I,
)


def git_environment(protocol="https"):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_ALLOW_PROTOCOL=protocol, GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_CONFIG_COUNT="3",
               GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull,
               GIT_CONFIG_KEY_1="http.followRedirects", GIT_CONFIG_VALUE_1="false",
               GIT_CONFIG_KEY_2="gc.auto", GIT_CONFIG_VALUE_2="0")
    return env


class Source:
    def __init__(self, url=UPSTREAM, protocol="https"):
        self.url = url
        self.env = git_environment(protocol)

    def git(self, args, cwd, log, *, timeout=120, network=False, objects=None):
        directory = Path(cwd).resolve()
        env = dict(self.env, GIT_CEILING_DIRECTORIES=str(directory.parent), LC_ALL="C")
        if objects:
            env["GIT_OBJECT_DIRECTORY"] = str(objects)
        log.flush()
        start = log.tell()
        try:
            return execute(["git", *args], directory, log, env=env, capture=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if network:
                raise TemporarilyUnavailable("连接官方仓库超时", interrupted=True) from None
            raise
        except CommandError:
            if network:
                with open(log.name, "rb") as errors:
                    end = errors.seek(0, os.SEEK_END)
                    errors.seek(max(start, end - 8192))
                    details = errors.read()
                    if TRANSIENT_GIT.search(details):
                        interrupted = bool(re.search(rb"early eof|disconnect|connection reset|remote end hung up|"
                                                     rb"curl (?:18|28|52|55|56|92)\b", details, re.I))
                        raise TemporarilyUnavailable("官方仓库网络暂不可用", interrupted) from None
            raise

    def latest(self, state, log):
        # ls-remote must not discover configuration from an ancestor checkout.
        with tempfile.TemporaryDirectory(prefix="remote-", dir=state) as directory:
            output = self.git(["ls-remote", "--exit-code", self.url, "HEAD"], directory, log, network=True)
        sha, name = output.split()
        if not COMMIT.fullmatch(sha) or name != "HEAD":
            raise ValueError("unexpected remote HEAD")
        return sha

    def missing(self, sha, work, log):
        listing = self.git(["rev-list", "--objects", "--no-object-names", "--missing=print", sha + "^{tree}"],
                           work, log, timeout=300)
        missing = [line[1:] for line in listing.splitlines() if line.startswith("?")]
        if any(not COMMIT.fullmatch(oid) for oid in missing):
            raise ValueError("invalid object ID in pinned tree")
        return sorted(missing)

    def fetch_objects(self, sha, work, objects, log):
        missing = self.missing(sha, work, log)
        if not missing:
            return
        batches = queue.Queue()
        for start in range(0, len(missing), BATCH_SIZE):
            batches.put(missing[start:start + BATCH_SIZE])
        stop = threading.Event()
        log_guard = threading.Lock()
        finished = 0
        log.write(("Fetching %d missing objects in batches of %d\n" % (len(missing), BATCH_SIZE)).encode())
        log.flush()

        def take_batch(repository, private_log, batch):
            for attempt, delay in enumerate(TRANSFER_ATTEMPTS):
                if delay:
                    time.sleep(delay)
                try:
                    self.git(["fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                              self.url, *batch], repository, private_log, objects=objects, timeout=180, network=True)
                    return
                except TemporarilyUnavailable as error:
                    if error.interrupted and len(batch) > 1:
                        half = len(batch) // 2
                        take_batch(repository, private_log, batch[:half])
                        take_batch(repository, private_log, batch[half:])
                        return
                    if attempt == len(TRANSFER_ATTEMPTS) - 1:
                        raise

        def lane():
            nonlocal finished
            with tempfile.TemporaryDirectory(prefix="git-transfer-", dir=work.parent) as repository:
                with open(Path(repository) / "transfer.log", "w+b") as private_log:
                    try:
                        self.git(["init", "--quiet", "--template="], repository, private_log)
                        while not stop.is_set():
                            try:
                                batch = batches.get_nowait()
                            except queue.Empty:
                                break
                            take_batch(repository, private_log, batch)
                            with log_guard:
                                old = finished
                                finished += len(batch)
                                if old // 1024 != finished // 1024 or finished == len(missing):
                                    log.write(("Fetched %d/%d missing objects\n" % (finished, len(missing))).encode())
                                    log.flush()
                    except BaseException:
                        stop.set()
                        raise
                    finally:
                        private_log.flush()
                        private_log.seek(0)
                        with log_guard:
                            shutil.copyfileobj(private_log, log)
                            log.flush()

        with concurrent.futures.ThreadPoolExecutor(max_workers=TRANSFER_WORKERS) as pool:
            tasks = [pool.submit(lane) for _ in range(min(TRANSFER_WORKERS, batches.qsize()))]
            for task in tasks:
                task.result()
        if self.missing(sha, work, log):
            raise ValueError("incomplete source tree after batched fetch")

    def resumable(self, sha, work, objects, log, marker):
        try:
            ready = marker.read_text() == sha + "\n"
        except FileNotFoundError:
            ready = False
        if not ready:
            marker.write_text(sha)
            self.git(["fetch", "--depth=1", "--filter=blob:none", "--no-tags", "--no-recurse-submodules",
                      "--no-write-fetch-head", self.url, sha], work, log, objects=objects, timeout=300, network=True)
            self.git(["rev-parse", "--verify", sha + "^{tree}"], work, log)
            marker.write_text(sha + "\n")
        # Only this commit's tree is required for checkout/build. Its parents can be outside the cache.
        shallow = work / ".git/shallow"
        bounds = set(shallow.read_text().splitlines()) if shallow.exists() else set()
        bounds.add(sha)
        shallow.write_text("\n".join(sorted(bounds)) + "\n")
        self.fetch_objects(sha, work, objects, log)

    def checkout(self, sha, directory, log):
        work = Path(directory).resolve()
        cache = private_dir(work.parent / "git-cache")
        objects = private_dir(cache / "objects")
        if "\n" in str(objects) or "\r" in str(objects):
            raise ValueError("unsupported Git object path")
        self.git(["init", "--quiet", "--template="], work, log)
        git = work / ".git"
        (git / "objects/info/alternates").write_text(str(objects) + "\n")
        seed = cache / "tip.json"
        previous = None
        if seed.is_file():
            metadata = json.loads(seed.read_bytes())
            previous, shallow = metadata["tip"], metadata["shallow"]
            if (not isinstance(previous, str) or not COMMIT.fullmatch(previous) or
                    not isinstance(shallow, list) or not all(isinstance(x, str) and COMMIT.fullmatch(x) for x in shallow)):
                raise ValueError("invalid private Git cache metadata")
            if shallow:
                (git / "shallow").write_text("\n".join(shallow) + "\n")
            self.git(["update-ref", "refs/dsh/previous", previous], work, log)
        marker = cache / "partial"
        if previous != sha:
            resume = marker.is_file() and marker.read_text().rstrip("\n") == sha
            if not resume:
                try:
                    self.git(["fetch", "--depth=1", "--no-tags", "--no-recurse-submodules",
                              "--no-write-fetch-head", self.url, sha], work, log, objects=objects,
                             timeout=45 * 60, network=True)
                except TemporarilyUnavailable as error:
                    if not error.interrupted:
                        raise
                    resume = True
            if resume:
                self.resumable(sha, work, objects, log, marker)
        self.git(["checkout", "--quiet", "--detach", "--no-recurse-submodules", sha], work, log)
        if self.git(["rev-parse", "--verify", "HEAD"], work, log) != sha:
            raise ValueError("checkout differs from selected commit")
        if previous != sha:
            shallow_file = git / "shallow"
            shallow = shallow_file.read_text().splitlines() if shallow_file.exists() else []
            with tempfile.NamedTemporaryFile(mode="w", dir=cache, delete=False) as tmp:
                json.dump({"tip": sha, "shallow": shallow}, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp.name, seed)
            marker.unlink(missing_ok=True)


@contextlib.contextmanager
def read_only_checkout(directory):
    # Source code runs later in Docker from git archive; the host build adapter reads this checkout.
    frozen_dirs = []
    try:
        for parent, folders, files in os.walk(directory):
            for name in files:
                path = Path(parent) / name
                mode = path.lstat().st_mode
                if stat.S_ISREG(mode):
                    path.chmod(mode & ~0o222)
            folder = Path(parent)
            mode = folder.stat().st_mode
            folder.chmod(mode & ~0o222)
            frozen_dirs.append(folder)
        yield
    finally:
        for folder in frozen_dirs:
            folder.chmod(folder.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)


def snapshot(request_id=None, action=None, source=None):
    return dict(schema=1, requestId=request_id, action=action, source=source,
                phase="idle", current=None, latest=None, available=None,
                message="等待检查更新", updatedAt=timestamp())


class Worker:
    def __init__(self, deployment, state, driver, at="04:00", source=None, clock=time.time, *, data_directory=None, docker_mailbox=False):
        self.deployment, self.state, self.driver = trusted_paths(deployment, state, driver, data_directory, docker_mailbox=docker_mailbox)
        self.data = None if docker_mailbox else data_root(self.deployment, data_directory)
        self.at = schedule_time(at)
        self.clock = clock
        self.source = source or Source()
        self.mail = None
        self.db = None
        self.lock = None
        self.operation_lock = None
        self.heartbeat_stop = threading.Event()
        self.heartbeat_lock = threading.Lock()
        self.heartbeat = None
        self.first_day = None
        self.retry_at = None

    def __enter__(self):
        try:
            self.lock = open(self.state / "worker.lock", "a")
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.operation_lock = open(self.state / "operation.lock", "a")
            if self.data is None:
                from volume_mailbox import VolumeMailbox
                self.mail = VolumeMailbox(self.state)
            else:
                self.mail = Mailbox(self.data)
            self.db = sqlite3.connect(self.state / "worker.sqlite")
            self.db.execute("CREATE TABLE IF NOT EXISTS jobs "
                            "(id TEXT PRIMARY KEY, action TEXT NOT NULL, day TEXT, "
                            "status TEXT NOT NULL, retry_at REAL, touched REAL NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            now = dt.datetime.fromtimestamp(self.clock())
            first = now.date() + dt.timedelta(days=int(now.strftime("%H:%M") >= self.at))
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('first_day', ?)", (first.isoformat(),))
            self.first_day = self.db.execute("SELECT value FROM settings WHERE key='first_day'").fetchone()[0]
            pending = self.db.execute("SELECT retry_at FROM jobs WHERE day=? ORDER BY touched DESC, rowid DESC LIMIT 1",
                                      (now.date().isoformat(),)).fetchone()
            self.retry_at = pending[0] if pending else None
            self.db.commit()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        self.heartbeat_stop.set()
        if self.heartbeat:
            self.heartbeat.join()
        if self.mail:
            self.mail.close()
        if self.db:
            self.db.close()
        if self.operation_lock:
            self.operation_lock.close()
        if self.lock:
            self.lock.close()

    def next_occurrence(self):
        now = dt.datetime.fromtimestamp(self.clock())
        if self.retry_at:
            retry = dt.datetime.fromtimestamp(self.retry_at)
            if retry.date() == now.date() and retry > now:
                return retry.astimezone().isoformat(timespec="seconds")
        start = dt.datetime.combine(now.date(), dt.time.fromisoformat(self.at))
        if start <= now:
            start += dt.timedelta(days=1)
        return start.astimezone().isoformat(timespec="seconds")

    def beat(self):
        with self.heartbeat_lock:
            beat = {"schema": 1, "updatedAt": timestamp(), "scheduled": True,
                    "scheduleTime": self.at, "nextCheckAt": self.next_occurrence()}
            if self.driver.name == "portable_deploy.py":
                storage = json.loads((self.state / "binding.json").read_bytes())["storage"]
                beat["storage"] = {kind: storage[kind]["type"] for kind in ("data", "workspace")}
            self.mail.write("worker.json", beat)

    def start_heartbeat(self):
        self.beat()

        def repeat():
            while not self.heartbeat_stop.wait(15):
                try:
                    self.beat()
                except OSError:
                    # A transient mailbox error must not interrupt rollback.
                    pass

        self.heartbeat = threading.Thread(target=repeat, daemon=True)
        self.heartbeat.start()

    def run_driver(self, operation, log, *args):
        timeouts = {"current": 90, "recover": 3600, "build": 7200, "validate": 3600, "deploy": 7200}
        env = dict(git_environment() if operation == "build" else os.environ, DSH_UPDATER_STATE=str(self.state), DSH_UPDATER_LOG=str(Path(log.name).resolve()))
        return execute([sys.executable, str(self.driver), operation, *map(str, args)],
                       self.deployment, log, env=env, capture=operation == "current",
                       timeout=timeouts[operation], pass_fds=(self.lock.fileno(), self.operation_lock.fileno()))

    def current(self, log):
        sha = self.run_driver("current", log)
        if sha and not COMMIT.fullmatch(sha):
            raise ValueError("invalid current source commit")
        return sha or None

    def record(self, item, day, retry_at=None):
        item["updatedAt"] = timestamp()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
                            (item["requestId"], item["action"], day,
                             json.dumps(item, ensure_ascii=False), retry_at, self.clock()))
        if day:
            self.retry_at = retry_at
            self.beat()
        self.mail.write("status.json", item)

    def phase(self, job, day, phase, message, retry_at=None):
        job.update(phase=phase, message=message)
        self.record(job, day, retry_at)

    @contextlib.contextmanager
    def operating(self, stop):
        # A new worker waits for installation. An old worker can still stop promptly.
        while not stop.is_set():
            try:
                fcntl.flock(self.operation_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                stop.wait(0.25)
        else:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(self.operation_lock, fcntl.LOCK_UN)

    def start(self):
        self.start_heartbeat()
        with open(self.state / "recovery.log", "ab") as log:
            try:
                self.run_driver("recover", log)
            except (CommandError, OSError, ValueError, subprocess.TimeoutExpired):
                item = snapshot()
                item.update(phase="blocked", message="宿主机恢复检查尚未通过，请查看私有 recovery.log")
                self.mail.write("status.json", item)
                raise RuntimeError("DSH host recovery failed; see private recovery.log") from None
        for raw, day in self.db.execute("SELECT status, day FROM jobs").fetchall():
            item = json.loads(raw)
            if item["phase"] not in TERMINAL:
                self.phase(item, day, "failed", "上次升级进程中断，宿主恢复检查已结束。请重新检查版本。")
        last = self.db.execute("SELECT status FROM jobs ORDER BY touched DESC, rowid DESC LIMIT 1").fetchone()
        self.mail.write("status.json", json.loads(last[0]) if last else snapshot())

    def due(self):
        now = dt.datetime.fromtimestamp(self.clock())
        if now.strftime("%H:%M") < self.at:
            return None
        day = now.date().isoformat()
        if day < self.first_day:
            return None
        row = self.db.execute("SELECT retry_at FROM jobs WHERE day=? ORDER BY touched DESC, rowid DESC LIMIT 1",
                              (day,)).fetchone()
        if row is None or (row[0] is not None and self.clock() >= row[0]):
            return day
        return None

    def run_job(self, request, day=None):
        job = snapshot(request["id"], request["action"], "scheduled" if day else "manual")
        previous = self.db.execute("SELECT status FROM jobs WHERE id=?", (request["id"],)).fetchone()
        if previous:
            recorded = json.loads(previous[0])
            if recorded["action"] != request["action"]:
                job.update(phase="failed", message="该请求编号已用于其他操作，请重新提交")
                self.mail.write("status.json", job)
            else:
                self.mail.write("status.json", recorded)
            return
        with open(self.state / (job["requestId"] + ".log"), "ab") as log:
            self.phase(job, day, "checking", "正在检查当前部署和官方远程仓库")
            try:
                try:
                    job["current"] = self.current(log)
                except CommandError as error:
                    if error.code == 69:
                        raise TemporarilyUnavailable("Docker 引擎尚未就绪") from None
                    raise
                job["latest"] = self.source.latest(self.state, log)
                available = job["current"] != job["latest"] if job["current"] else None
                job["available"] = available
                if job["action"] == "check" or available is False:
                    if available is None:
                        message = "已取得远程版本，当前部署的提交未知"
                    else:
                        message = "有可用更新" if available else "当前部署已是最新版本"
                    self.phase(job, day, "succeeded", message)
                    return
                sha = job["latest"]
                self.phase(job, day, "building", "正在获取已选定的源码并构建镜像")
                with tempfile.TemporaryDirectory(prefix="source-", dir=self.state) as work:
                    self.source.checkout(sha, work, log)
                    with read_only_checkout(work):
                        self.run_driver("build", log, sha, work)
                    self.phase(job, day, "validating", "正在隔离环境验证候选镜像")
                    self.run_driver("validate", log, sha)
                self.phase(job, day, "deploying", "正在复查服务、备份并部署已验证的镜像")
                self.run_driver("deploy", log, sha)
                actual = self.current(log)
                if actual != sha:
                    raise ValueError("deployed commit did not match")
                job.update(current=actual, available=False)
                self.phase(job, day, "succeeded", "部署已完成，当前运行版本已核实")
            except TemporarilyUnavailable as error:
                retry = self.clock() + BUSY_RETRY_SECONDS if day else None
                self.phase(job, day, "blocked", str(error) + ("，定时任务稍后重试" if day else "，请稍后重试"), retry)
            except CommandError as error:
                if error.code == 75 and job["phase"] == "deploying":
                    retry = self.clock() + BUSY_RETRY_SECONDS if day else None
                    msg = "服务正在使用，定时任务稍后重试" if day else "服务正在使用，请在空闲时重试"
                    self.phase(job, day, "blocked", msg, retry)
                elif error.code == 78 and job["phase"] == "deploying":
                    message = "更新未通过健康检查，已回滚到原部署"
                    if self.driver.name == "portable_deploy.py":
                        message += "。如已切换容器，期间产生的文件另存在备份收据所列的 changes 卷中。"
                    self.phase(job, day, "rolled-back", message)
                else:
                    self.failure(job, day, log, error)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                self.failure(job, day, log, error)

    def failure(self, job, day, log, error):
        stage = {"checking": "版本检查", "building": "源码获取或镜像构建", "validating": "候选镜像验证",
                 "deploying": "部署或健康检查"}[job["phase"]]
        log.write((str(error) + "\n").encode("utf-8", errors="replace"))
        if job["phase"] == "deploying":
            try:
                self.run_driver("recover", log)
            except (CommandError, OSError, ValueError, subprocess.TimeoutExpired) as recovery_error:
                log.write((str(recovery_error) + "\n").encode("utf-8", errors="replace"))
                self.phase(job, day, "blocked", "部署中断，宿主恢复尚未通过，升级服务将等待恢复。")
                raise RuntimeError("host recovery pending; see private task log") from None
        self.phase(job, day, "failed", stage + "失败，详情见宿主机的私有任务日志。")

    def tick(self):
        try:
            incoming = self.mail.request()
        except OSError:
            # Docker may be offline. Do not start a scheduled deployment without the inbox.
            return
        if incoming:
            request, identity = incoming
            if request:
                self.run_job(request)
                self.mail.acknowledge(identity)
                return
            self.mail.acknowledge(identity)
        day = self.due()
        if day:
            self.run_job({"id": str(uuid.uuid4()), "action": "upgrade"}, day)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--data-directory", type=Path, help="host bind mount containing the DSH profile")
    storage.add_argument("--docker-mailbox", action="store_true", help="use only the named volume's updater inbox")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--at", type=schedule_time, default="04:00")
    parser.add_argument("--once", action="store_true", help="process one pending request or due daily upgrade")
    args = parser.parse_args()
    os.umask(0o077)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Finish the current transaction if the supervisor allows a graceful shutdown.
        signal.signal(sig, lambda *_: stop.set())
    with Worker(args.deployment, args.state, args.driver, at=args.at, data_directory=args.data_directory, docker_mailbox=args.docker_mailbox) as worker:
        with worker.operating(stop) as active:
            if active:
                worker.start()
        while not stop.is_set():
            with worker.operating(stop) as active:
                if active:
                    worker.tick()
            if args.once or stop.wait(2):
                break


if __name__ == "__main__":
    main()
