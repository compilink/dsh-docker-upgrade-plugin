#!/usr/bin/env python3
"""Discover an installed DSH Web plugin on this Mac and register a user-approved worker."""
import argparse
import hashlib
import json
import io
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import subprocess
import sys
import tarfile
import time
import tempfile

import install
from worker import schedule_time

DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"
BUNDLE = "dsh-bundle-docker-upgrade"
TITLE = "DSH Docker 升级"
PUBLIC_IMAGES = frozenset({
    "ghcr.io/yangchuansheng/deepseek-harness", "ghcr.io/735876214/deepseek-harness-docker",
    "ghcr.io/obrige/dsh-docker", "ghcr.io/alliottech/deepseek-harness",
    "ghcr.io/runzhliu/deepseek-harness", "ghcr.io/openlistteam/dsh-docker",
})
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
COMPOSE_LABELS = ("project", "service", "project.working_dir", "project.config_files", "container-number", "oneoff", "config-hash")
LABEL_FIELDS = ",".join('"com.docker.compose.' + field + '":{{json (index .Config.Labels "com.docker.compose.'
                        + field + '")}}' for field in COMPOSE_LABELS)
INSPECT = ('{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
           '"ref":{{json .Config.Image}},"running":{{json .State.Running}},"ports":{{json .NetworkSettings.Ports}},'
           '"launch":[{{json .Config.Entrypoint}},{{json .Config.Cmd}}],"exposed":{{json .Config.ExposedPorts}},"mounts":{{json .Mounts}},"labels":{' + LABEL_FIELDS + '}}')
NPM_MANIFEST = "/app/node_modules/@deepseek-ai/dsh/package.json"
ARCHIVE_LIMIT = 160 * 1024
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})(?:-(?:alpha|beta|rc)\.[0-9]{1,5})?")



class SetupError(ValueError):
    """A user-facing explanation containing no raw Docker or filesystem data."""


class MissingCompose(SetupError):
    """The local configuration recorded by Compose is no longer available."""


class Cancelled(Exception):
    """The user dismissed a native confirmation."""


def run(args, timeout=25, cwd=None):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError("无法访问本机 Docker Desktop 或安装组件，请确认 Docker Desktop 已启动。") from exc
    if proc.returncode:
        raise SetupError("Docker Desktop 未能完成读取，请确认容器正在运行，当前用户有权访问 Docker。")
    return proc.stdout.strip()


def local_context(docker=DOCKER):
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_TLS_VERIFY") or os.environ.get("DOCKER_CERT_PATH"):
        raise SetupError("安装器仅连接本机 Docker Desktop，请在本机默认环境中打开。")
    context = run([docker, "context", "show"])
    if not NAME.fullmatch(context):
        raise SetupError("Docker context 名称无效。")
    endpoint = run([docker, "context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"])
    allowed = {"/var/run/docker.sock", str(Path.home() / ".docker/run/docker.sock")}
    if not endpoint.startswith("unix://") or endpoint[7:] not in allowed:
        raise SetupError("当前 Docker context 不是本机 Docker Desktop 的 Unix 套接字。")
    if run([docker, "--context", context, "info", "--format", "{{.OperatingSystem}}"]) != "Docker Desktop":
        raise SetupError("此安装包支持本机 Docker Desktop。")
    return context, endpoint


def directory(value, reason):
    path = Path(value)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise SetupError(reason)
    return path.resolve(strict=True)


def matches_manifest(data, package, name):
    try:
        target = package.resolve(strict=True)
        if data not in target.parents:
            return False
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 131072:
                return False
            manifest = json.loads(stream.read(131073))
        return isinstance(manifest, dict) and manifest.get("name") == name
    except (OSError, ValueError, RuntimeError):
        return False


def dsh_profile(data):
    profile = data / "profiles" / "web"
    return (matches_manifest(data, profile / "package.json", "dsh-profile-web")
            or matches_manifest(data, profile / "node_modules" / BUNDLE / "package.json", BUNDLE))


def manifest_version(archive):
    """Accept exactly the official package's regular manifest. Never extract archive paths."""
    if len(archive) > ARCHIVE_LIMIT:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as stream:
            member = stream.next()
            if (member is None or member.name not in ("package.json", "./package.json")
                    or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or not 0 < member.size <= 131072):
                return None
            with stream.extractfile(member) as file:
                value = json.load(file)
            if stream.next() is not None or not isinstance(value, dict) or value.get("name") != "@deepseek-ai/dsh":
                return None
            version = value.get("version")
            return version if isinstance(version, str) and VERSION.fullmatch(version) else None
    except (OSError, tarfile.TarError, ValueError, RecursionError, OverflowError):
        return None


def npm_version(info, docker, context):
    """Read a single fixed file from the running container without executing its code."""
    manifest = PurePosixPath(NPM_MANIFEST)
    mounts = [PurePosixPath(m["Destination"]) for m in info.get("mounts", []) if isinstance(m.get("Destination"), str)]
    if (PurePosixPath("/app/data") not in mounts or any(m == manifest or m in manifest.parents for m in mounts)
            or not re.fullmatch(r"[a-f0-9]{64}", info.get("id", ""))):
        return None
    args = [docker, "--context", context, "cp", info["id"] + ":" + NPM_MANIFEST, "-"]
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            deadline = time.monotonic() + 8
            archive = bytearray()
            with selectors.DefaultSelector() as poll:
                poll.register(process.stdout, selectors.EVENT_READ)
                while len(archive) <= ARCHIVE_LIMIT:
                    delay = deadline - time.monotonic()
                    if delay <= 0 or not poll.select(delay):
                        return None
                    block = os.read(process.stdout.fileno(), min(65536, ARCHIVE_LIMIT + 1 - len(archive)))
                    if not block:
                        if process.wait(timeout=max(0, deadline - time.monotonic())) == 0:
                            return manifest_version(archive)
                        return None
                    archive.extend(block)
        except (TimeoutError, subprocess.TimeoutExpired, OSError):
            return None
        finally:
            if process.poll() is None:
                process.kill()
    return None


def mount_type(info, path):
    matches = [m.get("Type") for m in info.get("mounts", []) if m.get("Destination") == path]
    if not matches:
        return "none"
    return matches[0] if len(matches) == 1 and matches[0] in {"volume", "bind", "tmpfs"} else "unknown"


def layout_detail(info, data_path):
    ports = info.get("ports") or {}
    return {"containerDataPath": data_path, "workspaceMountType": mount_type(info, "/workspace"),
            "sshExposed": "22/tcp" in (info.get("exposed") or {}) or "22/tcp" in ports,
            "sshPublished": bool(ports.get("22/tcp"))}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def storage_mount(info, target):
    rows = [item for item in info.get("mounts", []) if item.get("Destination") == target]
    if len(rows) != 1 or rows[0].get("RW") is not True:
        raise SetupError(target + " 需要一个可写的持久挂载。")
    mount = rows[0]
    if mount.get("Type") == "bind":
        path = mount.get("Source", "")
        # Docker Desktop may return the path inside its Linux VM for a macOS bind.
        for prefix in ("/host_mnt/", "/run/desktop/mnt/host/", "/mnt/host/"):
            if path.startswith(prefix):
                path = "/" + path[len(prefix):]
                break
        source = str(directory(path, target + " 对应的宿主目录在这台 Mac 上不可读。"))
    elif mount.get("Type") == "volume" and NAME.fullmatch(mount.get("Name", "")):
        source = mount["Name"]
    else:
        raise SetupError(target + " 需要绑定宿主目录，或由 Compose 指定可持续复用的 Docker 卷。")
    return {"type": mount["Type"], "target": target, "source": source}


def local_web_mapping(bindings):
    """Choose a reachable local origin without changing the operator's published addresses."""
    options = []
    for row in bindings:
        address = row.get("HostIp")
        value = str(row.get("HostPort", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,4}", value) or int(value) > 65535:
            continue
        if address in ("127.0.0.1", "0.0.0.0", ""):
            options.append((0 if address == "127.0.0.1" else 1, int(value), "127.0.0.1"))
        elif address in ("::1", "::"):
            options.append((2 if address == "::1" else 3, int(value), "::1"))
    if not options:
        raise SetupError("Web 端口只绑定到特定网络地址，不能从这台 Mac 的回环地址访问当前 DSH 所有者设置。")
    _, selected, loopback = min(options)
    return selected, loopback


def web_scope(bindings):
    if not bindings:
        return "unpublished"
    if any(row.get("HostIp") in ("0.0.0.0", "::", "") for row in bindings):
        return "published"
    if all(row.get("HostIp") in ("127.0.0.1","::1") for row in bindings):
        return "loopback"
    return "specific-interface"


def compose_service(binding, storage, container_config_hash):
    """Read one selected Compose service. Persist only a digest, never its environment or commands."""
    config = Path(binding["composeFile"])
    try:
        if config.stat().st_size > 2 * 1024 * 1024:
            raise SetupError("Compose 文件过大，无法验证当前服务。")
        command = [binding["docker"], "--context", binding["context"], "compose", "--project-directory",
                   binding["projectDirectory"], "--project-name", binding["project"], "--file", str(config), "config"]
        out = run([*command, "--format", "json", "--no-env-resolution", binding["service"]], cwd=binding["projectDirectory"])
        model = json.loads(out)
        service = model["services"][binding["service"]]
    except (SetupError, OSError, KeyError, ValueError, TypeError):
        raise SetupError("无法从本机 Compose 配置读取目标服务，请确认该部署仍由当前 Compose 文件管理。") from None
    if not isinstance(service, dict) or model.get("name") != binding["project"]:
        raise SetupError("Compose 文件与当前运行的项目或服务不一致。")
    if not isinstance(container_config_hash, str) or not re.fullmatch("[a-f0-9]{64}", container_config_hash):
        raise SetupError("运行容器缺少 Docker Compose 配置校验标签，无法确认它由这份文件创建。")
    hash_row = run([*command, "--hash=" + binding["service"], binding["service"]], cwd=binding["projectDirectory"]).split()
    if hash_row != [binding["service"], container_config_hash]:
        raise SetupError("Compose 文件或部署环境与创建这个容器时的配置不同。请先确认并应用独立修改，再重新检查。")
    if service.get("container_name") not in (None, binding["container"]):
        raise SetupError("Compose 中指定的容器名与正在运行的实例不一致。")
    declared_ref = service.get("image") or (binding["project"] + "-" + binding["service"])
    if binding["imageRef"] not in (declared_ref, declared_ref + ":latest"):
        raise SetupError("Compose 镜像引用与运行中的 DSH 不一致，请先应用或撤销独立的 Compose 更改。")
    for item in storage.values():
        entries = [v for v in service.get("volumes", []) if isinstance(v, dict) and v.get("target") == item["target"]]
        if len(entries) != 1 or entries[0].get("type") != item["type"] or entries[0].get("read_only") is True:
            raise SetupError(item["target"] + " 不是 Compose 中明确声明的可写持久挂载。请在此服务绑定原目录或指定原卷。")
        source = entries[0].get("source")
        if not isinstance(source, str):
            raise SetupError(item["target"] + " 使用自动分配的卷。请在 Compose 中明确指定现有卷，这样重建容器后才能保留同一份数据。")
        if item["type"] == "volume":
            volume = (model.get("volumes") or {}).get(source, {})
            actual = volume.get("name") or (binding["project"] + "_" + source)
        else:
            actual = str(Path(source).resolve(strict=True))
        if actual != item["source"]:
            raise SetupError(item["target"] + " 的 Compose 挂载与正在运行的容器不一致。")
    web = [v for v in service.get("ports", []) if isinstance(v, dict) and str(v.get("target")) == str(binding["containerPort"])
           and v.get("protocol", "tcp") == "tcp"]
    published = []
    for mapping in web:
        addr = mapping.get("host_ip", "")
        for bind_ip in ([addr] if addr else ["0.0.0.0", "::"]):
            published.append(dict(HostIp=bind_ip, HostPort=mapping.get("published")))
    if (local_web_mapping(published) != (binding["hostPort"], binding["loopbackHost"])
            or web_scope(published) != binding["webPublish"]):
        raise SetupError("Compose 声明的 Web 发布端口与当前实例不同，请先确认该部署使用的 Compose 文件。")
    # The release is allowed to change its own image reference. Other service settings must remain stable.
    stable = {key: value for key, value in service.items() if key != "image"}
    # Compare the physical resources, even where Compose refers to a named volume by a project-local alias.
    return digest([stable, storage])


def candidate(info, docker, context, endpoint, *, approved_compose=None):
    if not info.get("running"):
        return None
    labels = info.get("labels") or {}
    project = (labels.get("com.docker.compose.project") or "")
    service = (labels.get("com.docker.compose.service") or "")
    version = info.get("npmVersion") if "npmVersion" in info else npm_version(info, docker, context)
    data_path = "/app/data" if version else "/data"
    mounts = [item for item in info.get("mounts", []) if item.get("Destination") == data_path]
    if not mounts:
        return None
    if not version and (len(mounts) != 1 or mounts[0].get("Type") != "bind"):
        raise SetupError("旧布局当前需要绑定宿主目录；命名卷可做诊断。")
    data_mount = storage_mount(info, data_path)
    data = Path(data_mount["source"]) if data_mount["type"] == "bind" else None
    if not version and not dsh_profile(data):
        return None
    storage = {"data": data_mount}
    if version:
        storage["workspace"] = storage_mount(info, "/workspace")
        if (storage["workspace"]["type"], storage["workspace"]["source"]) == (data_mount["type"], data_mount["source"]):
            raise SetupError("数据目录和工作区需要各自独立的持久存储。")
    writable_dirs = [Path(item["source"]) for item in storage.values() if item["type"] == "bind"]
    if version and any(m.get("RW") and m.get("Destination") not in ("/app/data","/workspace") for m in info.get("mounts", [])):
        raise SetupError("该容器还有额外的可写挂载，当前备份程序不能覆盖所有持久化数据。")
    if len(writable_dirs) == 2 and (writable_dirs[0] in writable_dirs[1].parents or writable_dirs[1] in writable_dirs[0].parents):
        raise SetupError("数据目录和工作区的宿主目录有重叠，无法独立备份。")
    if not NAME.fullmatch(project) or not NAME.fullmatch(service):
        raise SetupError("识别到了 DSH 插件，此容器需要由 Docker Compose 管理。")
    files = (labels.get("com.docker.compose.project.config_files") or "").split(",")
    if len(files) != 1:
        raise SetupError("当前安装器支持由单份 Compose 文件定义的 DSH 服务。")
    override = version and approved_compose and approved_compose.get("composeSelected") is True
    if override and (approved_compose.get("project"), approved_compose.get("service")) != (project, service):
        raise SetupError("已选择的 Compose 文件属于另一项部署。")
    config = Path(approved_compose["composeFile"] if override else files[0])
    if not config.is_absolute() or not config.is_file():
        if version:
            raise MissingCompose("Docker 记录的 Compose 文件在本机不可读，可选择实际的 Compose 目录。")
        raise SetupError("Docker 记录的 Compose 文件在本机不可读。")
    if config.is_symlink():
        raise SetupError("需要使用实际的普通 Compose 文件。")
    config = config.resolve(strict=True)
    config_stat = config.stat()
    if any(folder in config.parents for folder in writable_dirs) or config_stat.st_uid not in (0, os.getuid()) or config_stat.st_mode & 0o022:
        raise SetupError("Compose 文件必须位于 DSH 数据目录外，且只能由部署所有者修改。")
    working_raw = approved_compose["projectDirectory"] if override else labels.get("com.docker.compose.project.working_dir")
    if version and not Path(working_raw or "").is_absolute():
        raise MissingCompose("Compose 工作目录在本机不可用，可选择实际的 Compose 目录。")
    if version and not Path(working_raw).is_dir():
        raise MissingCompose("Compose 工作目录在本机不可用，可选择实际的 Compose 目录。")
    working = directory(working_raw or "", "Compose 工作目录在本机不可读。")
    if any(working == folder or folder in working.parents for folder in writable_dirs):
        raise SetupError("Compose 工作目录需要独立于 DSH 可写的数据目录。")
    ports = [(port, bindings) for port, bindings in (info.get("ports") or {}).items() if bindings]
    if any(port == "3080/tcp" for port, _ in ports):
        port, bindings = "3080/tcp", info["ports"]["3080/tcp"]
    elif not version and len(ports) == 1 and ports[0][0] != "22/tcp":
        port, bindings = ports[0]
    else:
        raise SetupError("需要一个直接映射至 127.0.0.1 的 DSH Web 端口；SSH 端口不用于连接 DSH。")
    if not re.fullmatch(r"[1-9][0-9]{0,4}/tcp", port) or int(port[:-4]) > 65535:
        raise SetupError("DSH Web 端口无效。")
    host_port, loopback = local_web_mapping(bindings)
    name = info.get("name", "").lstrip("/")
    if (not NAME.fullmatch(name) or not re.fullmatch(r"[a-f0-9]{64}", info.get("id", ""))
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", info.get("image", ""))):
        raise SetupError("无法识别 DSH 容器和镜像。")
    binding = dict(docker=docker, context=context, endpoint=endpoint, project=project, service=service,
                   container=name, containerId=info["id"], composeFile=str(config), projectDirectory=str(working),
                   hostPort=host_port, loopbackHost=loopback, webPublish=web_scope(bindings),
                   containerPort=int(port[:-4]), imageId=info["image"], **layout_detail(info, data_path))
    if data is not None:
        binding["dataDirectory"] = str(data)
    if version:
        if (labels.get("com.docker.compose.oneoff") or "").lower() == "true" or labels.get("com.docker.compose.container-number") not in ("1", "1.0", None, ""):
            raise SetupError("当前目标必须是单实例 Compose 服务。")
        image_ref = info.get("ref")
        if not isinstance(image_ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}", image_ref):
            raise SetupError("运行中容器未提供可识别的镜像引用。")
        binding.update(storage=storage, npmVersion=version, imageRef=image_ref, launchConfigSHA256=digest(info.get("launch")))
        binding["composeConfigSHA256"] = compose_service(binding, storage, labels.get("com.docker.compose.config-hash"))
        if override:
            binding["composeSelected"] = True
    return binding


def public_image(ref):
    if not isinstance(ref, str):
        return None
    base = ref.partition("@")[0]
    registry, separator, tail = base.rpartition("/")
    image = registry + separator + tail.partition(":")[0]
    return image if image in PUBLIC_IMAGES else None


def public_image_info(info):
    image = public_image(info.get("ref"))
    version = info.get("npmVersion")
    if not image and not version:
        return None
    labels = info.get("labels") or {}
    compose = "none"
    if labels.get("com.docker.compose.project"):
        compose = "single-file" if len((labels.get("com.docker.compose.project.config_files") or "").split(",")) == 1 else "multiple-files"
    mounts = info.get("mounts") or []
    kinds = {item.get("Type") for item in mounts}
    data_path = "/app/data" if version else "/data"
    kind = mount_type(info, data_path)
    summary = {"compose": compose, "mountTypes": sorted(kinds & {"bind", "volume", "tmpfs"}),
               "data": kind if kind != "none" else "unconfirmed"}
    if image:
        summary["publicImage"] = image
    if version:
        summary.update(layout_detail(info, data_path), npmVersion=version)
        web = (info.get("ports") or {}).get("3080/tcp") or []
        summary["web"] = web_scope(web)
    return summary


def discover(docker=DOCKER, *, known_images=None, incomplete=None, approved_compose=None):
    context, endpoint = local_context(docker)
    command = [docker, "--context", context]
    identifiers = run([*command, "ps", "--no-trunc", "--format", "{{.ID}}"]).splitlines()
    choices, reasons = [], []
    for identifier in identifiers:
        if not re.fullmatch(r"[a-f0-9]{64}", identifier):
            continue
        try:
            info = json.loads(run([*command, "inspect", "--type", "container", "--format", INSPECT, identifier]))
            info["npmVersion"] = npm_version(info, docker, context)
            if known_images is not None:
                indication = public_image_info(info)
                if indication:
                    known_images[identifier] = indication
            approved = approved_compose if approved_compose and approved_compose.get("containerId") == identifier else None
            binding = candidate(info, docker, context, endpoint, approved_compose=approved)
            if binding:
                choices.append(binding)
        except MissingCompose as exc:
            reasons.append(str(exc))
            if incomplete is not None:
                incomplete.append((info, docker, context, endpoint))
        except SetupError as exc:
            reasons.append(str(exc))
        except (ValueError, OSError):
            reasons.append("有容器的 Docker 元数据无法读取。")
    return choices, list(dict.fromkeys(reasons))


def driver_check(binding, mode):
    driver = Path(__file__).with_name("portable_deploy.py")
    if not driver.is_file():
        raise SetupError("安装包不完整，缺少便携部署程序。")
    with tempfile.TemporaryDirectory(prefix="dsh-host-check-") as temp:
        root = Path(temp).resolve()
        exposed = [Path(m["source"]) for m in binding.get("storage", {}).values() if m["type"] == "bind"]
        if binding.get("dataDirectory"):
            exposed.append(Path(binding["dataDirectory"]))
        if any(root == data or data in root.parents for data in exposed):
            raise SetupError("安装预检需要位于 DSH 数据目录以外的临时目录。")
        install.atomic_file(root / "binding.json", json.dumps(binding).encode())
        try:
            result = subprocess.run([sys.executable, str(driver), mode], cwd=binding["projectDirectory"],
                                    env={**os.environ, "DSH_UPDATER_STATE": str(root)},
                                    capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SetupError("预检超时或无法启动，未注册系统服务。") from exc
        if result.returncode:
            explanation = result.stderr.strip() or "请核对 Docker、Compose 配置和 DSH 版本。"
            if result.returncode == 78 and not any(line.startswith("Traceback") for line in explanation.splitlines()):
                raise SetupError("当前部署未通过安装预检：" + explanation[:600])
            raise SetupError("当前部署未通过安装预检，未注册宿主服务。")
        if mode == "diagnose":
            return json.loads(result.stdout)


def preflight(binding):
    driver_check(binding, "preflight")


def native(lines, *arguments):
    args = ["/usr/bin/osascript"]
    for line in ["on run argv", *lines, "end run"]:
        args.extend(["-e", line])
    proc = subprocess.run([*args, "--", *arguments], capture_output=True, text=True)
    if proc.returncode:
        if "(-128)" in proc.stderr:
            raise Cancelled()
        raise SetupError("无法打开 macOS 确认窗口，请在已登录的桌面中打开安装器。")
    return proc.stdout.strip()


def name_for(binding):
    local = "[::1]" if binding.get("loopbackHost") == "::1" else "127.0.0.1"
    return f"{binding['project']} / {binding['service']}  ·  {binding['container']}  ·  {local}:{binding['hostPort']}"


def select(choices, reasons):
    if not choices:
        detail = "\n".join(reasons[:4])
        raise SetupError("未找到可安装的 DSH。请在这台运行 Docker Desktop 的 Mac 上，启动采用 Compose 部署的 DSH Web。" + ("\n" + detail if detail else ""))
    if len(choices) == 1:
        return choices[0]
    names = [name_for(item) for item in choices]
    selected = native(['set choice to choose from list argv with title "DSH Docker 升级" with prompt "选择要管理的 DSH 实例"',
                       'if choice is false then error number -128', 'return item 1 of choice'], *names)
    return choices[names.index(selected)]


def note(message):
    native(['display dialog (item 1 of argv) with title "DSH Docker 升级" buttons {"好"} default button "好"'], message)


def select_directory(record):
    info, docker, context, endpoint = record
    answer = native(['return POSIX path of (choose folder with prompt "选择此 DSH 项目的 Compose 目录")'])
    project_dir = directory(answer, "所选 Compose 目录不可读。")
    files = [project_dir / name for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
             if (project_dir / name).is_file()]
    if len(files) != 1:
        raise SetupError("所选目录需要有一份可明确识别的 Compose 文件。")
    labels = info["labels"]
    approved = dict(composeSelected=True, composeFile=str(files[0]), projectDirectory=str(project_dir),
                    project=labels["com.docker.compose.project"], service=labels["com.docker.compose.service"])
    return candidate(info, docker, context, endpoint, approved_compose=approved)


def select_deployment():
    incomplete = []
    choices, reasons = discover(incomplete=incomplete)
    if not incomplete:
        return select(choices, reasons)
    # A machine with both valid and moved configurations must not default to the wrong DSH.
    names = [name_for(item) for item in choices]
    for item, *_ in incomplete:
        labels = item["labels"]
        names.append(labels["com.docker.compose.project"] + " / " + labels["com.docker.compose.service"] +
                     " · " + item["id"][:12] + " · 选择 Compose 目录")
    index = 0
    if len(names) > 1:
        answer = native(['set choice to choose from list argv with title "DSH Docker 升级" with prompt "选择要管理的 DSH 实例"',
                         'if choice is false then error number -128', 'return item 1 of choice'], *names)
        index = names.index(answer)
    return choices[index] if index < len(choices) else select_directory(incomplete[index - len(choices)])


def onboard():
    binding = select_deployment()
    existing = install.existing_target(binding)
    if existing:
        note(f"此部署已经安装宿主升级服务，每日计划为 {existing}。原有服务和计划已保留，请返回 DSH 设置面板查看状态。")
        return
    preflight(binding)
    storage = binding.get("storage", {})
    mount_lines = [title + " " + ("Docker 卷 " if storage[key]["type"] == "volume" else "宿主目录 ") + Path(storage[key]["source"]).name
                   for key, title in (("data", "DSH 数据"), ("workspace", "工作区")) if key in storage]
    prompt = (f"为 {name_for(binding)} 安装宿主升级服务？\n"
              + "\n".join(["Compose 文件 " + Path(binding["composeFile"]).name,
                           "当前镜像 " + binding.get("imageRef", binding["imageId"][:19]), *mount_lines]) + "\n"
              "升级使用 DeepSeek Harness 官方 GitHub 默认分支的最新提交，保留容器现有的启动入口和 SSH 配置。\n"
              "宿主服务使用自己的本机授权与 DSH 通信，无需浏览器登录凭据。\n"
              "每日到点检查，并在空闲时升级。保留默认 04:00 或修改为本机时间（HH:MM）。")
    answer = native(['set reply to display dialog (item 1 of argv) with title "安装 DSH 宿主升级服务" default answer "04:00" '
                     'buttons {"取消", "安装"} default button "安装" cancel button "取消"',
                     'return text returned of reply'], prompt)
    try:
        at = schedule_time(answer)
    except argparse.ArgumentTypeError as exc:
        raise SetupError("每日升级时间必须是 00:00 至 23:59，例如 04:00。") from exc
    # The instance the user approved must still be the one Docker advertises.
    current, _ = discover(approved_compose=binding)
    if binding not in current:
        raise SetupError("确认期间 Docker 部署发生变化，请重新打开安装器。")
    preflight(binding)
    at = install.register_portable(binding, at)
    print("宿主升级服务已注册，每日计划为 " + at + "。DSH 设置面板将自动显示连接状态。")


def diagnostic_summary():
    """Collect only supportable categories, never addresses, names, digests or paths."""
    known_images = {}
    choices, reasons = discover(known_images=known_images)
    summaries = []
    for binding in choices:
        try:
            state = driver_check(binding, "diagnose")
            # User-provided image labels may contain private image tags or commits.
            summaries.append({"compose": "single-file", "data": binding.get("storage", {}).get("data", {}).get("type", "bind"), "web": binding.get("webPublish","loopback"),
                              "hasRegistryDigest": state.get("registryDigest") is True,
                              "declaresOfficialSource": state.get("declaresOfficialSource") is True,
                              "hasSourceRevision": state.get("hasSourceRevision") is True})
        except (OSError, ValueError, subprocess.SubprocessError):
            summaries.append({"compose": "single-file", "data": binding.get("storage", {}).get("data", {}).get("type", "bind"), "web": binding.get("webPublish","loopback"),
                              "imageMetadata": "unavailable"})
    for binding, summary in zip(choices, summaries):
        known = known_images.pop(binding["containerId"], None)
        summary.update({k: binding[k] for k in ("containerDataPath", "workspaceMountType", "sshExposed",
                                               "sshPublished", "npmVersion") if k in binding})
        if known and "publicImage" in known:
            summary["publicImage"] = known["publicImage"]
    return {"detected": len(choices), "instances": summaries,
            "otherPublicImages": list(known_images.values()), "notes": reasons}


def describe_npm(instance):
    types = {"bind": "宿主目录", "volume": "Docker 卷", "tmpfs": "临时内存", "none": "未挂载"}
    data = types.get(instance.get("data"), "未确认")
    workspace = types.get(instance.get("workspaceMountType"), "未确认")
    ssh = "已映射" if instance.get("sshPublished") else "已声明未映射" if instance.get("sshExposed") else "未声明"
    web = {"loopback": "仅回环映射", "published": "已对外映射", "unpublished": "未映射", "specific-interface": "指定接口"}.get(instance.get("web"), "未确认")
    return f"应用文件标记 @deepseek-ai/dsh {instance['npmVersion']}，/app/data：{data}，/workspace：{workspace}，Web：{web}，SSH：{ssh}。"


def diagnostic_dialog(report):
    summary = [f"识别到 {report['detected']} 个 DSH 实例。"]
    for index, instance in enumerate(report["instances"], 1):
        kind = "Docker 卷" if instance.get("data") == "volume" else "宿主目录"
        web = "Web 端口按原配置对外发布，本机通过回环地址访问" if instance.get("web") != "loopback" else "Web 端口仅在回环地址发布"
        detail = [f"实例 {index}：单个 Compose 文件，数据使用{kind}，{web}。"]
        if "hasRegistryDigest" in instance:
            flags = ("镜像带仓库摘要" if instance["hasRegistryDigest"] else "镜像无仓库摘要",
                     "声明官方源码" if instance["declaresOfficialSource"] else "未声明官方源码",
                     "有完整源码提交标签" if instance["hasSourceRevision"] else "无完整源码提交标签")
            detail.append("，".join(flags) + "。")
        else:
            detail.append("镜像元数据暂不可读取。")
        if "publicImage" in instance:
            detail.append("公开镜像仓库：" + instance["publicImage"])
        summary.append("".join(detail))
        if "npmVersion" in instance:
            summary.append(describe_npm(instance))
    for image in report.get("otherPublicImages", []):
        compose = {"none": "无 Compose 标记", "single-file": "单份 Compose", "multiple-files": "多份 Compose"}[image["compose"]]
        types = "、".join(image["mountTypes"]) or "未识别"
        if "npmVersion" in image:
            summary.append(f"已从应用目录识别 DSH（{compose}，挂载类型 {types}）。" + describe_npm(image))
        if "publicImage" in image:
            summary.append(f"另发现公开镜像 {image['publicImage']}（{compose}，挂载类型 {types}）。该部署未被识别为可绑定的 Web profile。")
    summary.extend(report["notes"])
    if not report["detected"] and not report["notes"] and not report.get("otherPublicImages"):
        summary.append("没有识别到包含 DSH Web profile 的运行中部署。")
    safe_text = "\n".join(summary)
    native(['set choice to display dialog (item 1 of argv) with title "DSH 宿主兼容性检查" '
            'buttons {"关闭", "复制诊断摘要"} default button "关闭"',
            'if button returned of choice is "复制诊断摘要" then set the clipboard to (item 1 of argv)'], safe_text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--detect-only", action="store_true", help="只输出经过脱敏的诊断结果，不注册服务")
    mode.add_argument("--detect-gui", action="store_true", help="在可复制的 macOS 弹窗查看诊断结果")
    args = parser.parse_args()
    if sys.platform != "darwin" or os.geteuid() == 0:
        print("请在运行 Docker Desktop 的 Mac 上，以已登录的普通用户打开。", file=sys.stderr)
        return 1
    os.umask(0o077)
    try:
        if args.detect_only or args.detect_gui:
            report = diagnostic_summary()
            if args.detect_gui:
                diagnostic_dialog(report)
            else:
                print(json.dumps(report, ensure_ascii=False))
            return 0 if report["detected"] or report["otherPublicImages"] else 1
        onboard()
    except Cancelled:
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, SetupError) else "无法完成这台 Mac 的部署或检查，原部署已保留。请检查宿主程序和当前用户权限。"
        print(message, file=sys.stderr)
        if not args.detect_only:
            try:
                note(message)
            except (ValueError, OSError, Cancelled):
                pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
