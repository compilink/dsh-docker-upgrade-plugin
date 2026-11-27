"""Build an official Git tree into the npm layout of a verified local Docker image."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import onboard
from portable_control import docker_cli
from worker import git_environment


class BuildError(ValueError):
    """A user-visible build diagnostic containing no Docker configuration values."""


SHA = re.compile(r"[a-f0-9]{40}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
HELPERS = Path(__file__).resolve().parent / "source-build"
RECIPE = "official-npm-layout-v1"
IMAGE_FACTS = (
    '{"id":{{json .Id}},"os":{{json .Os}},"arch":{{json .Architecture}},'
    '"onbuild":{{if (index .Config "OnBuild")}}true{{else}}false{{end}},'
    '"revision":{{with (index .Config "Labels")}}{{json (index . "org.opencontainers.image.revision")}}{{else}}null{{end}},'
    '"recipe":{{with (index .Config "Labels")}}{{json (index . "io.dsh-upgrade.source-recipe")}}{{else}}null{{end}}}'
)
LAYOUT_CHECK = """
const fs = require('node:fs')
const read = p => JSON.parse(fs.readFileSync(p, 'utf8'))
const cli = '/app/node_modules/@deepseek-ai/dsh'
if (fs.realpathSync('/app/apps/cli') !== cli || !fs.lstatSync('/app/apps/cli').isSymbolicLink()
    || read(cli + '/package.json').name !== '@deepseek-ai/dsh' || !fs.statSync(cli + '/lib/bin.js').isFile()
    || !fs.statSync('/usr/local/bin/dsh-entrypoint.sh').isFile()) process.exit(2)
const revision = process.argv[1]
if (revision === 'baseline') {
  const app = read('/app/package.json')
  if (Object.keys(app).join() !== 'dependencies' || Object.keys(app.dependencies ?? {}).join() !== '@deepseek-ai/dsh'
      || fs.existsSync('/app/.dsh-source-revision')) process.exit(3)
} else {
  const {execFileSync} = require('node:child_process')
  if (fs.readFileSync('/app/.dsh-source-revision', 'utf8').trim() !== revision ||
      execFileSync(process.execPath, ['/app/apps/cli/lib/bin.js','--version'], {encoding:'utf8'}).trim() !==
        read(cli + '/package.json').version) process.exit(4)
}
if (process.versions.node !== '24.22.3') process.exit(5)
"""


def image_facts(binding, image):
    result = json.loads(onboard.run([*docker_cli(binding), "image", "inspect", "--format", IMAGE_FACTS, image]))
    if (not IMAGE.fullmatch(result.get("id", "")) or result.get("onbuild") is not False
            or result.get("os") != "linux" or result.get("arch") not in {"amd64", "arm64"}):
        raise BuildError("源镜像需要是无 ONBUILD 钩子的 Linux amd64 或 arm64 镜像。")
    return result


def smoke(binding, image, revision=None):
    try:
        onboard.run([*docker_cli(binding), "run", "--rm", "--pull=never", "--network=none", "--read-only",
                     "--cap-drop=ALL", "--security-opt=no-new-privileges", "--tmpfs", "/tmp",
                     "--entrypoint", "/usr/local/bin/node", image, "-e", LAYOUT_CHECK, revision or "baseline"], timeout=65)
    except ValueError:
        raise BuildError("此镜像的 Node、入口、npm 依赖或已构建的 CLI 与支持的部署方式不一致。") from None


def paths(state, sha):
    if not SHA.fullmatch(sha):
        raise BuildError("需要完整的官方源码提交编号。")
    return state / ("portable-build-" + sha + ".json")


def write_record(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        name = handle.name
        try:
            handle.write(json.dumps(data, sort_keys=True).encode() + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            os.unlink(name)
            raise
    os.replace(name, path)
    parent = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def tracked(binding, state, facts):
    sha = facts.get("revision")
    if facts.get("recipe") != RECIPE or not isinstance(sha, str) or not SHA.fullmatch(sha):
        return None
    try:
        record = json.loads(paths(state, sha).read_bytes())
    except FileNotFoundError:
        raise BuildError("此源码镜像缺少当前宿主的构建记录。") from None
    if record.get("image") != facts["id"] or record.get("source") != sha or not IMAGE.fullmatch(record.get("root", "")):
        raise BuildError("源码镜像与当前宿主的构建记录不一致。")
    return record


def base(binding, state):
    facts = image_facts(binding, binding["imageId"])
    known = tracked(binding, state, facts)
    original = known["root"] if known else facts["id"]
    if image_facts(binding, original)["id"] != original:
        raise BuildError("本机原始运行镜像不可用，无法保留其启动和 SSH 配置。")
    return facts, original, known


def build(binding, state, sha, checkout):
    receipt = paths(state, sha)
    checkout = Path(checkout).resolve(strict=True)
    if checkout.parent != state or not checkout.name.startswith("source-"):
        raise BuildError("源码目录必须由当前宿主任务创建。")
    git = ["/usr/bin/git", "-C", str(checkout)]
    env = git_environment()
    head = subprocess.run([*git, "rev-parse", "--verify", "HEAD"], env=env, capture_output=True, check=True).stdout
    if head.decode().strip() != sha:
        raise BuildError("源码副本与已经核实的官方提交不一致。")
    facts, original, _ = base(binding, state)
    smoke(binding, original)
    key = hashlib.sha256((binding["project"] + "\0" + binding["service"] + "\0" + str(state)).encode()).hexdigest()[:16]
    tag = "dsh-source-candidate:" + key + "-" + sha
    # Keep the original image reachable even when Compose replaces its mutable service tag.
    foundation = "dsh-source-base:" + original.removeprefix("sha256:")
    cmd = docker_cli(binding)
    onboard.run([*cmd, "image", "tag", original, foundation])
    if image_facts(binding, foundation)["id"] != original:
        raise BuildError("构建期间本机基础镜像发生变化。")
    with tempfile.TemporaryDirectory(dir=state, prefix="context-") as tmp:
        context = Path(tmp)
        for name in ("Dockerfile", "pack.sh", "product.mjs", "apply-layer.mjs"):
            shutil.copyfile(HELPERS / name, context / name)
        subprocess.run([*git, "archive", "--format=tar", "--output=" + str(context / "source.tar"), sha],
                       env=env, check=True, timeout=240)
        docker_env = {key: value for key, value in os.environ.items()
                      if key not in {"DOCKER_CONTEXT", "DOCKER_HOST", "BUILDKIT_HOST", "BUILDX_BUILDER"}}
        result = subprocess.run([*cmd, "build", "--builder", "default", "--platform", "linux/" + facts["arch"], "--progress", "plain",
                                 "--build-arg", "BASELINE=" + foundation, "--build-arg", "REVISION=" + sha,
                                 "--build-arg", "ORIGINAL_BASE=" + original, "--iidfile", str(context / "image-id"),
                                 "--tag", tag, str(context)], env=docker_env, timeout=6800)
        if result.returncode:
            raise BuildError("官方源码构建未完成，请查看本机私有任务日志。")
        image = (context / "image-id").read_text().strip()
    built = image_facts(binding, tag)
    if built["id"] != image or built.get("recipe") != RECIPE or built.get("revision") != sha:
        raise BuildError("候选镜像与本次构建不符。")
    layers = ["image", "inspect", "--format", "{{json .RootFS.Layers}}"]
    before = json.loads(onboard.run([*cmd, *layers, original]))
    after = json.loads(onboard.run([*cmd, *layers, image]))
    if (image_facts(binding, foundation)["id"] != original or not before or
            after[:len(before)] != before or len(after) <= len(before)):
        raise BuildError("候选镜像未继承已核实的本机运行镜像。")
    record = {"image":image, "tag":tag, "source":sha, "root":original, "baseline":facts["id"], "validated":False}
    write_record(receipt, record)
    return record


def candidate(binding, state, sha, *, validated=False):
    record = json.loads(paths(state, sha).read_bytes())
    if (record.get("source") != sha or record.get("baseline") != binding["imageId"]
            or not IMAGE.fullmatch(record.get("image", "")) or (validated and record.get("validated") is not True)):
        raise BuildError("宿主镜像与本次候选验证记录不一致。")
    facts = image_facts(binding, record["image"])
    if facts["id"] != record["image"] or facts.get("recipe") != RECIPE or facts.get("revision") != sha:
        raise BuildError("候选镜像未通过身份验证。")
    return record
