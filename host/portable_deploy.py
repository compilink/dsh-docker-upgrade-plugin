#!/usr/bin/env python3
"""Run source builds and transactions for a locally bound npm-layout DSH deployment."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import onboard
from portable_control import docker_cli
import portable_transaction as transaction
import source_build


class Unsupported(ValueError):
    """A verified deployment cannot use an available upgrade recipe."""


SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_INFO = (
    '{"id":{{json .Id}},"registryDigest":{{if .RepoDigests}}true{{else}}false{{end}},'
    '"revision":{{with (index .Config "Labels")}}{{json (index . "org.opencontainers.image.revision")}}{{else}}null{{end}},'
    '"upstream":{{with (index .Config "Labels")}}{{if eq (index . "org.opencontainers.image.source") '
    '"https://github.com/deepseek-ai/deepseek-harness"}}true{{else}}false{{end}}{{else}}false{{end}}}'
)


def read_binding(state):
    """Use the selected host state, independent of container-writable profile settings."""
    if not state.is_absolute() or state.resolve(strict=True) != state:
        raise Unsupported("宿主绑定必须位于本机私有目录。")
    owner = state.lstat()
    if not stat.S_ISDIR(owner.st_mode) or owner.st_uid != os.getuid() or owner.st_mode & 0o077:
        raise Unsupported("宿主绑定目录需要仅当前用户可访问。")
    descriptor = os.open(state / "binding.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 16384 or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise Unsupported("宿主绑定文件的权限或格式不正确。")
        binding = json.load(file)
    if not isinstance(binding, dict) or binding.get("docker") != onboard.DOCKER:
        raise Unsupported("宿主绑定必须使用本机 Docker Desktop。")
    if not re.fullmatch(r"[a-f0-9]{64}", str(binding.get("containerId", ""))):
        raise Unsupported("容器绑定无效，请重新选择正在运行的 DSH。")
    return binding


def verify_context(binding):
    context, endpoint = onboard.local_context()
    if context != binding.get("context") or endpoint != binding.get("endpoint"):
        raise Unsupported("本机 Docker Desktop 的连接目标已变化，请重新选择 DSH。")
    return context, endpoint


def inspect_binding(binding):
    """Re-check the selected container and return only shareable metadata categories."""
    context, endpoint = verify_context(binding)
    command = docker_cli(binding)
    instance = json.loads(onboard.run([*command, "inspect", "--type", "container", "--format",
                                      onboard.INSPECT, binding["containerId"]]))
    current = onboard.candidate(instance, onboard.DOCKER, context, endpoint, approved_compose=binding)
    if current is None or current != binding:
        raise Unsupported("已选容器、镜像、Compose 或挂载已变化，请重新选择 DSH。")
    image = json.loads(onboard.run([*command, "image", "inspect", "--format", IMAGE_INFO, current["imageId"]]))
    if image.get("id") != current["imageId"]:
        raise Unsupported("Docker 未返回该容器正在使用的镜像。")
    revision = image.get("revision")
    return {
        "compose": "single-file", "data": binding.get("storage", {}).get("data", {}).get("type", "bind"),
        "web": binding.get("webPublish", "loopback"),
        "registryDigest": image.get("registryDigest") is True,
        "declaresOfficialSource": image.get("upstream") is True,
        "hasSourceRevision": bool(isinstance(revision, str) and SHA.fullmatch(revision)),
    }


def supports_layout(binding):
    if binding.get("containerDataPath") != "/app/data" or not binding.get("npmVersion"):
        raise Unsupported("当前镜像未采用受支持的 DSH npm 安装布局，已保留原部署。")


def dispatch(operation, parameters, binding, state):
    if operation == "recover":
        # An incomplete transaction can temporarily have a different container ID.
        if (state / transaction.JOURNAL).exists():
            verify_context(binding)
        transaction.recover(binding, state)
        return 0
    if operation == "current":
        try:
            onboard.run([*docker_cli(binding), "info", "--format", "{{.ServerVersion}}"], timeout=12)
        except onboard.SetupError:
            # A scheduled check can retry once Docker Desktop starts.
            return 69
    report = inspect_binding(binding)
    if operation == "diagnose":
        print(json.dumps(report, ensure_ascii=False))
        return 0
    supports_layout(binding)
    if operation == "current":
        facts = source_build.image_facts(binding, binding["imageId"])
        known = source_build.tracked(binding, state, facts)
        if known:
            print(known["source"])
        return 0
    if operation == "build":
        source_build.build(binding, state, *parameters)
        return 0
    if operation == "preflight":
        _, root, known = source_build.base(binding, state)
        source_build.smoke(binding, root)
        if known:
            source_build.smoke(binding, binding["imageId"], known["source"])
        transaction.preflight(binding, state)
        return 0
    sha = parameters[0]
    record = source_build.candidate(binding, state, sha, validated=operation == "deploy")
    if operation == "validate":
        record["validated"] = False
        source_build.write_record(source_build.paths(state, sha), record)
        source_build.smoke(binding, record["image"], sha)
        transaction.validate(binding, state, record["image"], sha)
        record["validated"] = True
        source_build.write_record(source_build.paths(state, sha), record)
        return 0
    return transaction.deploy(binding, state, record["image"], sha)


def main(args):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("diagnose", "preflight", "current", "recover", "build", "validate", "deploy"))
    parser.add_argument("parameters", nargs="*")
    options = parser.parse_args(args)
    try:
        expected = {"build": 2, "validate": 1, "deploy": 1}.get(options.operation, 0)
        if len(options.parameters) != expected or (expected and not SHA.fullmatch(options.parameters[0])):
            raise Unsupported("宿主操作参数或官方提交编号无效。")
        state = Path(os.environ.get("DSH_UPDATER_STATE", ""))
        return dispatch(options.operation, options.parameters, read_binding(state), state)
    except (Unsupported, source_build.BuildError, onboard.SetupError, transaction.TransactionError) as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        # Docker's diagnostics, filesystem paths and arbitrary image labels can contain private data.
        print("无法只读核实选中的 DSH 容器及镜像，请确认 Docker Desktop 和 DSH 正在运行。", file=sys.stderr)
    # Only a verified rollback completed by transaction.deploy may return 78 during deployment.
    return 1 if options.operation in ("deploy", "recover") else 78


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
