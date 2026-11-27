#!/bin/sh
set -eu
umask 077
export PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin
cd -P -- "$(dirname -- "$0")"
for python in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$python" ] && "$python" -E -s -B -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
        exec "$python" -E -s -B ./host/onboard.py "$@"
    fi
done
/usr/bin/osascript -e 'display alert "DSH Docker 升级" message "此安装器需要 Python 3.9 或更高版本。安装 Apple 命令行开发工具或 Python 3 后，请重新打开。"'
exit 1
