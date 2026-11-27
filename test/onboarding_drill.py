"""Opt-in real Docker Desktop and LaunchAgent drill, using only synthetic profiles and a synthetic worker driver."""
import argparse
from contextlib import contextmanager
import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.request import Request, urlopen
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "host"))
import install
import onboard

FIXTURE_SERVER = """
import {createServer} from 'node:http';
import {apply} from './index.js';
const routes = new Map();
const fixtureOwner = {};
apply({
  effect(f) { f() },
  profileContext:{dir:'/app/data/profiles/web'},
  webServer:{protocol:'http:',register(r){routes.set(r.path,r.handler)}},
  agents:{list(){return []}},emit(){},
  connection:{browserPeer:fixtureOwner,admit(){return {peer:fixtureOwner}}}
});
createServer((req,res)=>{
  if((req.method==='GET' && req.url==='/local/dsh-docker-upgrade-status') ||
     (req.method==='POST' && req.url==='/local/dsh-docker-upgrade-check')) {
    return routes.get(req.url)(req,res);
  }
  res.writeHead(404).end();
}).listen(3080,'0.0.0.0');
"""


def output(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=kwargs.pop("timeout", 150), **kwargs)
    if result.returncode:
        raise RuntimeError("fixture command failed: " + " ".join(str(v) for v in args[1:4]) +
                           " (" + str(result.returncode) + ")")
    return result.stdout.strip()


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def compose_fixture_files(folder, project, base, data_volume):
    build = folder / "build"
    build.mkdir()
    for name in ("index.js", "portable-control.js", "ssh-idle.js"):
        shutil.copy(REPO / name, build / name)
    (build / "package.json").write_text('{"type":"module"}')
    (build / "manifest").write_text('{"name":"@deepseek-ai/dsh","version":"0.2.2-rc.5"}')
    (build / "server.mjs").write_text(FIXTURE_SERVER)
    (build / "Dockerfile").write_text("FROM " + base + """
WORKDIR /test-fixture
COPY index.js portable-control.js ssh-idle.js package.json server.mjs ./
COPY manifest /app/node_modules/@deepseek-ai/dsh/package.json
RUN mkdir -p /app/data/dsh-source-updater /app/apps /workspace && chown -R node:node /app/data /workspace && ln -s /app/node_modules/@deepseek-ai/dsh /app/apps/cli
USER node
ENTRYPOINT ["/usr/local/bin/node","/test-fixture/server.mjs"]
EXPOSE 22 3080
VOLUME ["/app/data","/workspace"]
""")
    data = folder / "data"
    work = folder / "work"
    data.mkdir(mode=0o755)
    work.mkdir(mode=0o755)
    mapping = port()
    tag = project + "-image:local"
    resources = {"data": {"type":"volume","source":"store","target":"/app/data"} if data_volume else
                  {"type":"bind","source":str(data), "target":"/app/data"},
                 "work": {"type":"bind","source":str(work), "target":"/workspace"} if data_volume else
                  {"type":"volume","source":"store", "target":"/workspace"}}
    model = {"services":{"web":{"image":tag, "build":{"context":str(build)},
                                "ports":[{"target":3080,"published":str(mapping),"host_ip":"127.0.0.1"}],
                                "volumes":list(resources.values())}}, "volumes":{"store":{}}}
    compose = folder / "compose.yaml"
    compose.write_text(json.dumps(model))
    return compose, tag, mapping


@contextmanager
def fixture(data_volume=True, base="node:24.22.3-bookworm"):
    """Start and remove a loopback-only fixture identified by an unpredictable Compose project name."""
    if sys.platform != "darwin" or os.getuid() == 0:
        raise RuntimeError("This drill needs Docker Desktop and an ordinary macOS user.")
    docker = onboard.DOCKER
    context, endpoint = onboard.local_context(docker)
    cmd = [docker,"--context",context]
    output([*cmd,"image","inspect","--format","{{.Id}}",base])
    project = "dsh-fixture-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="dsh-mac-drill-", dir="/private/tmp") as tmp:
        folder = Path(tmp)
        file, tag, web_port = compose_fixture_files(folder, project, base, data_volume)
        command = [*cmd,"compose","--project-name",project,"--project-directory",str(folder),"-f",str(file)]
        try:
            output([*cmd, "build","--network","none","--pull=false","-t",tag,str(folder/"build")], timeout=300)
            output([*command,"up","--no-build","--pull","never","--detach","web"])
            cid = output([*command,"ps","-q","web"])
            info = json.loads(output([*cmd,"inspect","--format",onboard.INSPECT,cid]))
            selected = onboard.candidate(info,docker,context,endpoint)
            if not selected or selected["containerId"] != cid or selected["project"] != project:
                raise RuntimeError("The detector did not bind the isolated web service.")
            yield selected, folder, web_port
        finally:
            output([*command,"down","--volumes","--remove-orphans"])
            if subprocess.run([*cmd,"image","inspect",tag],capture_output=True).returncode==0:
                output([*cmd,"image","rm",tag])
            if output([*cmd,"ps","-aq","--filter","label=com.docker.compose.project="+project]):
                raise RuntimeError("Fixture container left behind.")
            if output([*cmd,"volume","ls","-q","--filter","label=com.docker.compose.project="+project]):
                raise RuntimeError("Fixture volume left behind.")


def drill(data_volume, detect_only=False):
    with fixture(data_volume) as (binding, folder, port_number):
        assert binding["storage"]["data"]["type"] == ("volume" if data_volume else "bind")
        assert binding["storage"]["workspace"]["type"] == ("bind" if data_volume else "volume")
        if detect_only:
            return
        app = folder/"host-state"
        agents = folder/"test-agents"
        label = "io.github.compilink.dsh-onboarding-drill." + uuid.uuid4().hex
        job = f"gui/{os.getuid()}/{label}." + install.binding_id(binding)
        original_copy = install.copy_portable

        def fixture_driver(source, destination):
            original_copy(source, destination)
            # The real worker owns heartbeat and mailbox I/O. This driver can never perform an image update.
            (destination/"portable_deploy.py").write_text("import sys\nsys.exit(0 if sys.argv[1:] == ['recover'] else 78)\n")

        with patch.object(install,"APP",app), patch.object(install,"LABEL",label), \
                patch.object(install,"agents_directory",return_value=agents), patch.object(install,"copy_portable",side_effect=fixture_driver):
            at = (datetime.datetime.now() + datetime.timedelta(hours=4)).strftime("%H:%M")
            try:
                assert install.register_portable(binding, at) == at
                deadline = time.monotonic()+45
                while time.monotonic()<deadline:
                    try:
                        with urlopen(f"http://127.0.0.1:{port_number}/local/dsh-docker-upgrade-status",timeout=2) as response:
                            result=json.load(response)
                        if result.get("workerOnline") is True and result.get("scheduleTime") == at:
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.25)
                else:
                    log = next(app.glob("instances/*/state/service.log"), None)
                    hints = ("volume transport" if data_volume else "host bind") + ", log=" + str(bool(log))
                    if log:
                        # This is an entirely synthetic isolated process log; only report exception class or argparse state.
                        lines = log.read_text(errors="replace").splitlines()
                        hints += ", class=" + next((line.split(":")[0] for line in reversed(lines)
                                                   if line.startswith(("TypeError:","ValueError:","PermissionError:","RuntimeError:","usage:"))),"none")
                    raise RuntimeError("The real DSH plugin status route did not see the fixture heartbeat (" + hints + ").")
                base_url = f"http://127.0.0.1:{port_number}/local/dsh-docker-upgrade"
                cmd = [onboard.DOCKER,"--context",binding["context"],"exec","--user","node",binding["containerId"],
                       "/usr/local/bin/node","-e"]
                ids = set()
                for _ in range(2):
                    with open(app/"instances"/install.binding_id(binding)/"state"/"operation.lock","a") as guard:
                        fcntl.flock(guard,fcntl.LOCK_EX)
                        with urlopen(base_url+"-status",timeout=3) as res:
                            before = json.load(res)
                        confirm = Request(base_url+"-check",method="POST",data=b"",headers={
                            "Origin": f"http://127.0.0.1:{port_number}",
                            "X-DSH-Upgrade-Revision":before["revision"]})
                        with urlopen(confirm, timeout=3) as res:
                            assert res.status == 202
                            request_id = json.load(res)["id"]
                        assert request_id not in ids
                        ids.add(request_id)
                        output([*cmd,"const s=require('fs').lstatSync('/app/data/dsh-source-updater/request.json');"
                                     "if(s.uid!==process.getuid() || (s.mode&511)!==384 || s.nlink!==1)process.exit(1)"])
                    deadline = time.monotonic()+18
                    while time.monotonic()<deadline:
                        with urlopen(base_url+"-status",timeout=3) as res:
                            done = json.load(res)
                        if (not done["busy"] and done["status"]["requestId"] == request_id
                                and done["status"]["phase"] == "failed"):
                            # The inert fixture driver declines "current" before any Git or image operation.
                            break
                        time.sleep(0.2)
                    else:
                        raise RuntimeError("The worker did not persist and acknowledge the fixture request.")
                    output([*cmd,"if(require('fs').existsSync('/app/data/dsh-source-updater/request.json'))process.exit(1)"])
                assert install.register_portable(binding, "01:12") == at
            finally:
                subprocess.run([install.LAUNCHCTL,"bootout",job],capture_output=True)
                deadline=time.monotonic()+15
                while subprocess.run([install.LAUNCHCTL,"print",job],capture_output=True).returncode==0:
                    if time.monotonic()>deadline:
                        raise RuntimeError("Test worker still registered.")
                    time.sleep(0.1)


if __name__=="__main__":
    cmd=argparse.ArgumentParser(description=__doc__)
    cmd.add_argument("--detect-only",action="store_true")
    args=cmd.parse_args()
    for named in (True, False):
        drill(named,args.detect_only)
        print(("data volume + workspace bind" if named else "data bind + workspace volume") +
              (": detection verified" if args.detect_only else ": worker online, two node-owned 0600 requests acknowledged"))
