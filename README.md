# DSH Docker 升级

在 DeepSeek Harness 的 **设置 → DSH Docker 升级** 中检查上游源码，查看定时计划，或请求本机升级服务部署更新。页面显示当前提交、上游提交和最近一次执行结果。

## 在线安装

在运行 DSH 的环境中，将下面的固定版本安装到 Web profile。也可以把命令中的完整安装包 URL 粘贴到支持 URL 安装的 DSH 插件管理器。

```sh
dsh plugin --profile web add --ignore-scripts https://github.com/compilink/dsh-docker-upgrade-plugin/releases/download/v0.1.1/dsh-bundle-docker-upgrade.tgz
```

在 DSH 空闲时通过现有服务管理器重新加载服务，打开 **设置 → DSH Docker 升级**。已有的宿主服务继续使用它自己的计划和数据目录。安装包和对应的 SHA-256 校验文件可从 [v0.1.1 Release](https://github.com/compilink/dsh-docker-upgrade-plugin/releases/tag/v0.1.1) 下载。

## 适用环境

- DSH Web 需要提供 WebServer、Connection、Agents 和 ProfileContext 服务。使用本机回环地址直接登录，以当前浏览器所有者身份访问设置页。区分浏览器与 API 身份的 DSH 版本还需要宿主的浏览器所有者身份服务。
- 手动检查、部署和定时任务需要预先配置的宿主升级程序。本版本已在 macOS 用户级 LaunchAgent 和 Docker Desktop 管理的 DSH 部署上验证。
- 公开发行包提供 DSH 设置页及服务端接口。宿主升级程序需要由部署管理员单独配置。首次在其他机器上安装时，页面可能显示“宿主机升级服务未就绪”，此时两个按钮均不可用。
- 检查与升级请求通过 DSH 数据目录下的 `dsh-source-updater` 文件队列提交。浏览器只能请求这两个固定动作。计划、上游仓库、构建、备份和容器切换由宿主程序管理。安装包无生命周期安装脚本，插件无需挂载 Docker socket。

## 使用

点「检查更新」可查看本地和上游提交。点「立即升级」并确认后，宿主在空闲时处理升级。部署过程中页面可能短暂断开，恢复后会自动读取执行结果。定时任务在页面关闭后由宿主继续运行。页面中的下次检查时间以浏览器本地时区显示。

## 验证发行包

```sh
node --test test/client.test.js test/server.test.js
python3 tools/verify_release.py
```

校验程序核对发行包的 SHA-256、文件清单以及每个文件与当前版本源码的一致性。
