# XDocker 实例备份与 PowerX 应用备份

PowerX 的应用内备份中心位于 `/ops/backup`，已有备份策略、定时任务、恢复演练与告警接口。它使用 PowerX 自己的用户、租户和权限。XDocker 的“实例备份”管理 Docker 部署外层，不借用 PowerX 管理员密码或绕过其权限。

## 当前支持

- 明确登记的 PowerX 实例：创建、列表、私有下载、SHA256 校验、数据库隔离恢复验证。
- PostgreSQL 使用运行实例相同镜像内的 `pg_dump --format=custom`。不复制或压缩正在运行的 PostgreSQL 数据目录。
- 同时保存 Redis RDB、config（含签名/加密密钥）、上传文件、插件及运行状态文件；排除日志、临时目录和应用自身的旧备份文件，避免递归打包。
- 创建时进入实例维护窗口：只暂停该实例原来运行的前端和后端，数据库/Redis 保持运行；完成或失败后恢复原状态。其他网站、FRP 和实例不暂停。
- 手动保留清理：输入保留成功备份份数并确认，至少保留最新一份。失败/中断备份不被自动删掉。
- 管理服务重启会停止自己登记的未完成辅助容器，并尝试恢复由它暂停的应用；无法恢复时明确标记失败，需通过 SSH 检查。

## 目录与隐私

```text
data/instance-backups/<instance>/<id>/
  database.dump        PostgreSQL 自定义格式逻辑备份
  redis.rdb            Redis 校验通过的快照
  config.tar.gz        配置、加密/签名密钥、Setup 草稿
  runtime.tar.gz       上传、插件和运行状态
  manifest.json        状态、镜像、校验、恢复验证结果
  backup.tar.gz        可下载的组合包
```

目录 700、备份包与清单 600，仅可信服务器管理员使用。组合包含真实业务数据和私钥，当前不做自动加密或异地上传；通过 SSH 私有管理入口下载后自行安全保管。公开 Docker 镜像不含这些备份、运行配置或用户数据。

## 部署管理功能

本地完成验证并推送仓库，服务器同步相同 SHA；选择通过完整工作流的 Admin 镜像，再执行：

```bash
cd ~/workspace/XDocker
sudo sh scripts/admin.sh init
# 在 services/admin/.env 选择已验证的新 ADMIN_IMAGE
sudo sh scripts/admin.sh start
```

`init` 写入实际 `ADMIN_HOST_ROOT` 并准备私有备份目录，不更换访问令牌。管理服务以部署用户 UID 运行，辅助容器只获得备份需要的固定挂载和命令，没有通用 shell/任意路径 API。

## 页面操作

1. 打开 XDocker 私有管理页，进入“实例备份”。选择明确登记的实例，点击“创建实例备份”。
2. 阅读维护窗口说明，输入完整实例名（例如 `powerx-dev`），确认后等待任务结果。失败时先看“操作记录”，不要把有文件生成当作完成。
3. 状态“已完成”后可校验、下载。下载响应提供 `X-Backup-SHA256`，页面也显示组合包 SHA256。
4. “隔离恢复验证”需要输入完整备份 ID。它用同一 PostgreSQL 镜像，在无网络、无公网端口的临时数据库执行 `pg_restore --exit-on-error`；结束后删除临时容器/存储，不覆盖运行实例。
5. 在“保留策略”填写 1–100 份并确认清理；清理只影响超过保留份数的旧成功备份。
6. “打开 PowerX 备份中心”进入应用内策略管理。未完成 Setup 时该应用中心还不可用，应先安装并用自己的 PowerX 账号登录。

## 恢复与扩展边界

隔离数据库恢复验证不是生产恢复。完整恢复须先选择新的实例目录与固定镜像，验证组合包及组成文件，恢复 config 和运行文件，再向新的 PostgreSQL 导入逻辑备份。Redis RDB 可单独恢复；任务队列状态恢复可能重新执行任务，应根据业务确认。验收业务数据、文件和密钥一致后再切换入口。

不提供点击覆盖当前数据库的按钮。当前也不自动设置定时全实例备份、异地存储、加密密钥托管或 PITR；应用内数据库定时计划继续由 PowerX 备份中心管理。后续增加这些能力时，应沿用实例隔离、明确权限、操作记录和恢复验收。

## 已完成的运行验证

2026-10-08，Admin 镜像 `sha-ec70aab2a18d758164490639d04d26f5b8366831` 在真实 `powerx-dev` 实例创建备份 `25fd8c0b0039dd54e2094a20edc3161a`，状态为 ready。组合包 667163 字节，SHA256 为 `44c51e3b5f83352c0e47f202bde65cd6d63d63bcb9e8e3c0ab35f3540262665d`。PostgreSQL 隔离恢复验证成功，认证下载的内容与 SHA256 一致，匿名下载返回 401；维护窗口结束后前后端恢复健康，原实例数据未替换。

本地 75 项检查与 CI 的真实 PostgreSQL/Redis 备份、恢复演练通过。浏览器实际点击流程尚未单独验收；当前实例仍使用原已安装数据，切换到空白 Setup 实例前需明确选择保留方式。

参考：[PostgreSQL 一致性逻辑备份](https://www.postgresql.org/docs/16/backup-dump.html)、[pg_dump 自定义格式](https://www.postgresql.org/docs/16/app-pgdump.html)。
