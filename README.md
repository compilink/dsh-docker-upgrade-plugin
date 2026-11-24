# DSH Docker 升级

在 DeepSeek Harness 的 **设置 → DSH Docker 升级** 中检查 DSH 主程序的上游提交，手动请求升级 Docker 部署，或查看宿主机的定时升级计划。页面显示当前提交、上游提交、执行阶段和最近结果。

**使用前需要已有宿主升级服务。** v0.1.1 的公开安装包包含 DSH 设置页和服务端接口。构建镜像、验证、备份、切换容器及执行定时任务由部署管理员另行配置的宿主程序负责。已验证的组合为 macOS 用户级 LaunchAgent 和 Docker Desktop。如果目标是让刚安装的插件生效，可以使用 [DSH 应用插件更新](https://github.com/compilink/dsh-apply-updates-plugin) 重启当前 DSH 服务。

## 在线安装

1. 在运行 DSH 的环境中，将固定版本安装到 Web profile。支持 URL 安装的插件管理器也可以直接使用命令中的安装包地址。

   ```sh
   dsh plugin --profile web add --ignore-scripts https://github.com/compilink/dsh-docker-upgrade-plugin/releases/download/v0.1.1/dsh-bundle-docker-upgrade.tgz
   ```

2. 等 DSH 空闲后，通过现有服务管理器重新加载服务。通过本机回环地址登录 DSH Web，打开 **设置 → DSH Docker 升级**。
3. 点击 **检查更新**，确认页面显示本地提交与上游提交。已有宿主服务继续使用原来的计划和持久化数据目录。

发布页提供安装包及其 SHA-256 校验文件。[v0.1.1 Release](https://github.com/compilink/dsh-docker-upgrade-plugin/releases/tag/v0.1.1)

## 检查、升级与定时执行

| 页面操作 | 执行结果 |
| --- | --- |
| **检查更新** | 宿主读取上游提交并与本地提交比较。检查本身不会切换容器。 |
| **立即升级** | 确认后提交升级请求。页面会显示任务、通道或插件安装占用的原因。开始部署后，可以查看构建、验证、备份、切换及恢复状态。网页短暂断开后会自动重连。 |
| **查看定时计划** | 每日执行时刻由宿主配置，关闭页面后仍可运行。下次检查时间按浏览器所在时区显示。 |

**如果显示「宿主机升级服务未就绪」**，请检查宿主程序是否运行、是否与 DSH 使用同一个数据目录，以及宿主写入的运行状态。服务就绪后页面会自动更新。

## 权限与存储

Web profile 需要提供 `webServer`、`connection`、`agents` 和 `profileContext`。API 只接受通过本机回环地址直接登录的浏览器所有者的同源操作。区分浏览器与 API 身份的 DSH 部署还需提供兼容的浏览器所有者识别服务。

插件通过 DSH 持久化数据目录下的 `dsh-source-updater/` 与宿主交换心跳和执行结果，并提交「检查」或「升级」两种固定请求。上游仓库、调度时间、镜像和备份目录都由宿主程序管理。发行包没有安装期脚本，DSH 容器无需挂载 Docker socket。

## 开发验证

运行设置页和服务端接口测试。

```sh
node --test test/client.test.js test/server.test.js
```

核对固定发行包时，在对应的发布标签源码目录运行以下命令。脚本会校验 SHA-256、包内文件清单以及各文件与该标签源码的字节一致性。

```sh
python3 tools/verify_release.py
```
