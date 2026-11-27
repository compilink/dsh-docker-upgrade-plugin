"""Build a reproducible macOS archive from reviewed host sources."""
import argparse
from pathlib import Path
import sys
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'host'))
from install import PORTABLE_FILES, portable_source

NAME = "DSH-Docker-Updater-macOS"


def build(output, diagnostic=False):
    folder = "DSH-Docker-Deployment-Check" if diagnostic else NAME
    files = {f'host/{name}': (portable_source(ROOT / 'host', name), 0o755 if name.endswith('.sh') else 0o644)
             for name in PORTABLE_FILES}
    files['安装 DSH 宿主升级服务.command'] = (ROOT / 'tools/Install DSH Updater.command', 0o755)
    with ZipFile(output, "w") as archive:
        for name, (path, mode) in sorted(files.items()):
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"Missing installer source: {name}")
            contents = path.read_bytes()
            if diagnostic and name.endswith(".command"):
                name = "查看 DSH 部署.command"
                first, body = contents.split(b"\n", 1)
                contents = first + b"\nset -- --detect-gui\n" + body
            entry = ZipInfo(f"{folder}/{name}", date_time=(2025, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (0o100000 | mode) << 16
            archive.writestr(entry, contents)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    build(options.output, options.diagnostic)
