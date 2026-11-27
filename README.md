# DSH Docker 升级

在 DeepSeek Harness Web 的设置面板中检查官方 GitHub 默认分支、升级本机 Docker Compose 服务或查看下次定时升级。每次升级都固定到检查时取得的完整 Git 提交。插件持续使用原来的 Web profile、DSH 数据和工作区。

## 在 Mac 上安装

1. 从 DSH 插件市场搜索 `dsh-docker-upgrade-plugin`，安装到 Web profile。市场加载新版后，在运行 Docker Desktop 的 Mac 上通过本机地址登录 DSH，打开 **设置 → DSH Docker 升级**。
2. 点 **安装 Mac 宿主升级服务**。解压下载的文件，在这台 Mac 上打开 **安装 DSH 宿主升级服务.command**。安装器使用当前用户的 Docker Desktop、Git 和 Python 3.9 或更高版本。源码构建在容器内进行。
3. 核对确认窗口中的 Compose 服务、Web 地址和每日升级时间。只有一个符合条件的实例时，确认一次即可安装。多个实例会列出供选择。Compose 文件移动后可用系统文件夹选择器指定新位置，安装器会与正在运行的容器重新核对。
4. 回到设置面板，看到 **宿主升级服务已连接** 后即可点 **检查更新**。同一实例重复安装会保留现有服务和定时计划。用户登录 Mac、Docker Desktop 正在运行时，定时任务在后台执行。

Web 管理器支持安装包地址时也可以使用固定发行包。插件、Mac 安装器和 SHA-256 校验文件随同一版本发布。[v0.2.0 Release](https://github.com/compilink/dsh-docker-upgrade-plugin/releases/tag/v0.2.0)

```sh
dsh plugin --profile web add --ignore-scripts https://github.com/compilink/dsh-docker-upgrade-plugin/releases/download/v0.2.0/dsh-bundle-docker-upgrade.tgz
```

## 自动识别的部署

安装器从本机 Docker Desktop 和运行中的 Compose 容器识别服务、镜像、映射端口、数据目录或卷，以及工作区。支持的源码构建方式以已经部署的 Node 24.22.3 Linux 镜像为基础，该镜像的 DSH 由 npm 安装在 `/app/node_modules/@deepseek-ai/dsh`，`/app/apps/cli` 指向该安装。原镜像的入口脚本和 SSH 配置由该镜像提供。安装器在本机核对部署信息，用户无需上传 Dockerfile、启动脚本或 Compose 文件。

数据和工作区分别挂载在 `/app/data` 和 `/workspace`，可以使用独立的 Mac 目录或 Compose 命名卷。安装器校验当前 Compose 文件、目标服务以及实际挂载。检测到多个配置文件、额外持久化挂载、共享的可写数据或其他无法验证的布局时会显示原因。已有的旧版专用宿主服务仍可使用设置面板。

## 更新和恢复

**检查更新**读取官方默认分支的当前提交。首次从 npm 镜像迁移时，该镜像可能只有 npm 版本号，面板会显示原部署的源码提交未知。宿主构建新版本后可以按提交比较后续更新。

**立即升级**和定时升级使用相同流程。宿主在独立构建阶段获取指定提交、编译及打包 DSH，再将运行文件装入原镜像的 npm 目录布局。使用独立数据副本启动候选服务，通过检查后才开始替换。切换时复核 DSH 会话、插件任务和 SSH 活动，并备份数据及工作区。后台任务占用服务时，升级会延后。

宿主通过 DSH 自身核对默认模型、已注册的提供商和已有会话的历史摘要，再验证 Web 入口及 SSH 主机公钥。启动验收失败时会尝试恢复原镜像，恢复前另存失败实例的数据。Compose、镜像引用或数据归属被其他程序改动时，恢复会停下来并保留记录。宿主日志和任务记录在当前用户的 `~/Library/Application Support/DSH Source Updater/` 下。每个实例的 `state/backups/` 保存备份收据，记录 Docker 备份卷和回滚镜像；收据和会话校验均留在本机。

## 权限和开发验证

首次注册需要在 Docker 主机上由当前用户确认。Web 插件只接受本机同源、已登录所有者发出的检查和升级请求。宿主使用本机 Docker Desktop 的 Unix socket，DSH 容器无需挂载该 socket。宿主与 DSH 的停机确认独立于浏览器登录凭据。

```sh
node --test test/*.test.js
python3 -B -m unittest discover -s test -p 'test_*.py'
python3 tools/verify_release.py
```
