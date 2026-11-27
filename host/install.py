#!/usr/bin/env python3
"""Install the DSH source updater as a macOS per-user LaunchAgent."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from worker import private_dir, schedule_time, trusted_paths

LABEL = "io.github.compilink.dsh-source-updater"
LAUNCHCTL = "/bin/launchctl"
APP = Path.home() / "Library" / "Application Support" / "DSH Source Updater"


def copy_host(source, destination):
    def ignore(directory, names):
        ignored = {n for n in names if n in {".git", "__pycache__", ".DS_Store", "tests"} or n.endswith(".pyc")}
        for name in set(names) - ignored:
            path = Path(directory) / name
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("host bundle contains unsupported entry: " + name)
        return ignored

    shutil.copytree(source, destination, ignore=ignore)
    for script in ("worker.py", "deploy.py"):
        if not (destination / script).is_file():
            raise ValueError("incomplete host bundle, missing " + script)


def plist(host, deployment, state, at, *, label=LABEL, driver="deploy.py", data=None, volume=False):
    path = os.pathsep.join([
        "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        str(Path.home() / ".docker" / "bin"), "/Applications/Docker.app/Contents/Resources/bin",
    ])
    arguments = [sys.executable, str(host / "worker.py"), "--deployment", str(deployment),
                 "--state", str(state), "--driver", str(host / driver), "--at", at]
    if volume:
        arguments.append("--docker-mailbox")
    elif data is not None:
        arguments.extend(["--data-directory", str(data)])
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(state),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "EnvironmentVariables": {"PATH": path, "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1"},
        "StandardOutPath": str(state / "service.log"), "StandardErrorPath": str(state / "service.log"),
        "Umask": 0o077,
    })


def atomic_file(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        name = temp.name
        try:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        except BaseException:
            os.unlink(name)
            raise
    try:
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def bootstrap(domain, destination):
    # bootout may return while launchd still holds the previous label. Reuse the
    # same plist and user domain during that short unregister window.
    for attempt in range(12):
        proc = subprocess.run([LAUNCHCTL, "bootstrap", domain, str(destination)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode == 0:
            return
        if proc.returncode != 5 or attempt == 11:
            raise RuntimeError("launchctl bootstrap failed (" + str(proc.returncode) + ")")
        time.sleep(0.5)


def install(deployment, state, at):
    home = private_dir(APP)
    deployment, state, _ = trusted_paths(deployment, state, Path(__file__).with_name("deploy.py"))
    releases = private_dir(home / "releases")
    release = Path(tempfile.mkdtemp(prefix="host-", dir=releases))
    host = release / "host"
    copy_host(Path(__file__).resolve().parent, host)
    destination = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("LaunchAgent plist must be a regular file")
    old = destination.read_bytes() if destination.exists() else None
    domain = "gui/" + str(os.getuid())
    service = domain + "/" + LABEL
    payload = plist(host, deployment, state, at)
    if old is not None:
        args = plistlib.loads(old)["ProgramArguments"]
        if "--state" not in args or Path(args[args.index("--state") + 1]) != state:
            raise ValueError("use the installed worker's private state directory when upgrading it")
    with open(state / "operation.lock", "a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        active = subprocess.run([LAUNCHCTL, "print", service], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL).returncode == 0
        if active and old is None:
            raise ValueError("the LaunchAgent label is already registered by another installation")
        if active:
            subprocess.run([LAUNCHCTL, "bootout", service], check=True)
        try:
            atomic_file(destination, payload)
            bootstrap(domain, destination)
        except BaseException:
            if old is None:
                destination.unlink(missing_ok=True)
            else:
                atomic_file(destination, old)
                if active:
                    bootstrap(domain, destination)
            raise
    print("Installed " + LABEL + "; daily upgrade at " + at + " local time.")
    print("Private host state and task logs: " + str(state))


def agents_directory():
    return Path.home() / "Library" / "LaunchAgents"


def binding_id(binding):
    # Compose can recreate a container or move to a new image without changing ownership.
    identity = [binding[key] for key in ("composeFile", "project", "service")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def data_identity(binding):
    storage = binding.get("storage", {}).get("data")
    if storage:
        return storage["type"], storage["source"]
    return "bind", str(Path(binding["dataDirectory"]).resolve())


def existing_target(binding):
    """Find this user's existing updater for precisely this data directory."""
    directory = agents_directory()
    label = LABEL + "." + binding_id(binding)
    data = data_identity(binding)
    paths = [directory / (LABEL + ".plist"), *sorted(directory.glob(LABEL + ".*.plist"))]
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("已有的升级服务文件权限或归属不正确，已保留原文件。")
        try:
            definition = plistlib.loads(path.read_bytes())
            args = definition["ProgramArguments"]
            old_root = Path(args[args.index("--deployment") + 1])
            old_data = Path(args[args.index("--data-directory") + 1]) if "--data-directory" in args else old_root / "data"
            plan = schedule_time(args[args.index("--at") + 1])
            old_state = Path(args[args.index("--state") + 1])
            if definition["Label"] != path.stem or not all(isinstance(arg, str) for arg in args):
                raise ValueError()
            old_driver = Path(args[args.index("--driver") + 1]).resolve(strict=True)
            if ((APP / "releases").resolve() not in old_driver.parents and
                    (APP / "instances").resolve() not in old_driver.parents):
                raise ValueError()
        except (ValueError, KeyError, TypeError, IndexError, OSError, argparse.ArgumentTypeError, plistlib.InvalidFileException):
            raise ValueError("已有同名的升级服务无法识别，已保留原配置。") from None
        old = None
        identity = ("bind", str(old_data.resolve()))
        if path.stem != LABEL:
            old = json.loads((old_state / "binding.json").read_bytes())
            identity = data_identity(old)
        if identity != data:
            if path.stem == label:
                raise ValueError("此 Compose 服务已经关联其他数据目录，已保留原绑定。")
            continue
        if path.stem != LABEL:
            if (path.stem != label or any(old.get(key) != binding.get(key) for key in
                                       ("project", "service", "composeFile", "storage", "dataDirectory"))):
                raise ValueError("此数据目录已绑定另一个 Compose 服务，已保留原绑定。")
        service = "gui/" + str(os.getuid()) + "/" + path.stem
        if subprocess.run([LAUNCHCTL, "print", service], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode:
            raise ValueError("此部署已有宿主服务，其 LaunchAgent 当前未加载。已保留原配置和每日计划 " + plan + "。")
        return plan
    return None


PORTABLE_FILES = (
    "onboard.py", "install.py", "worker.py", "portable_deploy.py", "portable_transaction.py",
    "portable_control.py", "portable_inventory.py", "control-client.cjs", "volume_mailbox.py", "mailbox-client.cjs", "ssh-idle.js",
    "source_build.py", "source-build/Dockerfile", "source-build/apply-layer.mjs", "source-build/pack.sh",
    "source-build/product.mjs",
)


def portable_source(source, relative):
    """Resolve only the explicit public release files in the packaged and development layouts."""
    if relative not in PORTABLE_FILES:
        raise ValueError("宿主发行包包含未登记的文件。")
    path = source / relative
    if relative == "ssh-idle.js" and not path.exists():
        path = source.parent / relative
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("安装包缺少可信的宿主程序 " + relative)
    return path


def copy_portable(source, destination):
    destination.mkdir(mode=0o700)
    for relative in PORTABLE_FILES:
        path = portable_source(source, relative)
        target = destination / relative
        target.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o700 if relative.endswith(".sh") else 0o600)


def register_portable(binding, at):
    """Register one fixed Compose service; a repeat preserves the installed plan."""
    at = schedule_time(at)
    data = Path(binding["dataDirectory"]).resolve(strict=True) if binding.get("dataDirectory") else None
    identity = data_identity(binding)
    if (identity[0] == "bind" and str(data) != identity[1]) or (data is not None and identity[0] != "bind"):
        raise ValueError("数据卷和宿主绑定不一致。")
    exposed = [Path(m["source"]).resolve() for m in binding.get("storage", {}).values() if m["type"] == "bind"]
    if data is not None:
        exposed.append(data)
    protected = (APP.resolve(), agents_directory().resolve())
    if any(m == location or m in location.parents or location in m.parents for m in exposed for location in protected):
        raise ValueError("DSH 持久挂载与宿主程序或 LaunchAgents 目录重叠。请把数据和工作区绑定到独立目录。")
    home = private_dir(APP)
    with open(home / "install.lock", "a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        old_plan = existing_target(binding)
        if old_plan:
            return old_plan
        key = binding_id(binding)
        label = LABEL + "." + key
        instances = private_dir(home / "instances")
        instance = private_dir(instances / key)
        state = private_dir(instance / "state")
        directory = agents_directory()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = directory / (label + ".plist")
        domain = "gui/" + str(os.getuid())
        if destination.is_symlink() or destination.exists() or subprocess.run(
                [LAUNCHCTL, "print", domain + "/" + label], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL).returncode == 0:
            raise ValueError("服务名称已被占用，保留了已有服务。")
        source = Path(__file__).resolve().parent
        release = Path(tempfile.mkdtemp(prefix="release-", dir=instance))
        host = release / "host"
        record = state / "binding.json"
        if record.exists() or record.is_symlink():
            raise ValueError("已有安装记录与系统服务不一致，已保留现有数据。")
        try:
            copy_portable(source, host)
            payload = plist(host, binding["projectDirectory"], state, at, label=label,
                            driver="portable_deploy.py", data=data, volume=bool(binding.get("storage")))
            atomic_file(record, json.dumps(binding, ensure_ascii=False).encode())
            atomic_file(destination, payload)
            bootstrap(domain, destination)
        except BaseException:
            # Only undo the new private release and records written by this attempt.
            destination.unlink(missing_ok=True)
            record.unlink(missing_ok=True)
            shutil.rmtree(release)
            raise
    return at


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True,
                        help="existing trusted DSH Compose deployment directory (contains data/)")
    parser.add_argument("--state", type=Path, default=APP / "state")
    parser.add_argument("--at", type=schedule_time, default="04:00")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("this installer requires macOS launchd")
    os.umask(0o077)
    install(args.deployment, args.state, args.at)


if __name__ == "__main__":
    main()
