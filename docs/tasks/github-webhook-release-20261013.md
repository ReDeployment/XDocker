# GitHub 观察与 Webhook 独立发布审查

范围：仅 XDocker 的六仓库只读观察、签名接收、持久队列、私有通知界面、Webhook HTTPS 入口与证书登记。用户“fix”后先完成本地发布准备，不执行 Git 写入、镜像发布、服务器状态变更或其他项目修改；不使用本机 Docker。

当前主工作区还包含升级准备/只读预览、备份维护保护等未提交改动。共享 app.py、app.js、Dockerfile 不能直接整文件提交，否则会混入升级功能。本轮从已提交 HEAD 构造独立候选：只移植 GitHub imports、配置、初始化、API、页面/事件处理和对应打包/测试；备份、版本管理、升级代码保留主工作区原样。

## 如何生成可审查结果

```sh
python3 scripts/prepare-github-release.py
```

输出在 Git 忽略的 `data/release-previews/github-watch-20261013/`：

- `tree/`：已提交基线加本轮 GitHub 功能，供独立测试；无私有 .env、令牌、证书或运行数据。
- `candidate.patch`：后续获批后可在该基线的独立工作树应用，含必要新文件。
- `manifest.json`：基线 SHA、变更文件 SHA256、补丁 SHA256 及验证状态。

只读取 Git，不创建分支、改索引、提交或推送。已有输出拒绝覆盖；重新准备时使用新 `--output data/release-previews/名称`。提取边界、基线或依赖改变时失败，不猜测合并。候选不包含升级模块、Schema、升级路由/导航、备份维护锁及相关回归变更；原工作区的未提交开发内容保留。

## 发布与部署顺序

1. 核对候选差异和私有配置，执行独立候选的 Python/页面/语法检查；记录实际结果。
2. 获得提交/推送授权后，在独立任务分支应用已核对补丁、创建 PR；不直接 push develop。
3. PR 只验证，不发布。获准合并后，main 的 Admin 与 Receiver 双架构容器验收通过，再发布已测试镜像。
4. 部署授权后服务器同步相同 SHA；真实 IP 和 Secret 只写私有配置，保留已有证书/服务，签发 hooks HTTPS。
5. 由维护者先配置 PowerX 的 repository webhook，验证通知及重投去重，再接其余项目。GitHub 写 API 不在本轮授权范围。

本地候选通过不等于容器、公网 TLS、GitHub 真实投递或服务器部署通过。候选改变后必须重新准备并验证，不能继续应用旧补丁。

## 本地验收记录（2026-10-13）

- 基线：`3f5f24a69be0dd19334bf7d36c1780fb160bed9d`，只读 fetch 后 HEAD/origin/main 一致。
- 独立候选 46 个变更文件；已核对 Admin 无升级 imports/routes/navigation，backups.py、image_versions.py、Admin requirements.txt 与基线逐字节一致。主工作区的升级/备份开发改动保留，没有创建分支、改索引或提交。
- Python 3.10.13（现有依赖）及 Python 3.13.5（CI 同 minor 的独立虚拟环境、仓库固定依赖）均运行 204 项：199 通过、5 项真实 Docker 检查明确跳过；页面模拟、JS/Shell 语法通过。
- 从干净公开基线回放 candidate.patch，所有文件逐字节匹配已验证候选；公开 IP 模板为空，不包含运行 .env、令牌、证书私钥或 SQLite 数据。
- 首次默认 Python 3.14 环境缺 Flask/PyYAML，结果不计入通过；改用明确解释器和隔离依赖后完成验证。Python 3.13 暴露既有 Admin `db()` 返回 SQLite Connection、`with db()` 未显式 close 的 ResourceWarning；本轮保持基线实现，作为后续 Admin 问题记录，不将该修改混进发布候选。
- 尚未执行 GitHub CI、实际容器/Nginx 验收、镜像发布、服务器同步、证书签发或六项目 webhook 配置；本机 Docker 未使用。

最终待审查补丁与哈希位于 Git 忽略的 `data/release-previews/github-watch-20261013-final/`。生成器会同步保留这份审查文档，部署指南见 [Webhook](../guides/admin/github-webhook.md)。
