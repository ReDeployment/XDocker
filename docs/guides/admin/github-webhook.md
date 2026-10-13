# GitHub Webhook 接收与控制台通知

状态：本地实现，尚未发布镜像或部署。覆盖六个固定项目；沿用 [GitHub 观察指南](github-watch.md) 的仓库映射。下列部署步骤在批准提交、发布和服务器部署后执行；本机无需 Docker。用户已新增 DNS；本轮尚未创建 GitHub Webhook、签发此域名证书或生成真实密钥。

## 服务如何协作

```text
GitHub repository webhook
  → 专用 HTTPS 域名 /github/webhook/<项目名>
  → 共用 Nginx：仅开放 POST 路径，其他路径 404
  → github-webhook 独立容器：原始字节 HMAC-SHA256 验签
  → 私有持久队列
  → Admin 从磁盘导入
  → 私有管理网页「研发事件」的通知收件箱
```

接收器只挂载队列和只读密钥目录；没有 Docker socket、Admin 访问令牌、GitHub API token、PowerX 配置或业务数据库。接收器使用内部 `github-ingress` 网络，无宿主机映射端口；Nginx 接入该网络。Admin 保持独立 `admin-control` 网络及 SSH 私有访问，只共享队列目录，不共享密钥。

目录结构：

```text
services/github-webhook/       接收器源码、Dockerfile、Compose、公开配置模板
sites/github-webhook/          专用域名入口，默认未启用
data/github-webhook/secrets/   <项目名>.secret，逐仓库独立密钥，600/400
data/github-webhook/inbox/     queue.sqlite3，接收凭据与待导入事件，600
data/admin/github/            watch.sqlite3，观察快照、通知、已读及导入去重
```

容器内目录分别是接收器 `/secrets`、`/inbox` 和 Admin `/github-inbox`。私有目录 `700`，同一运行 UID/GID 读写队列。初始化以根 `.env` 文件的所有者设置 UID/GID；与已有 Admin 运行用户不一致时须核对服务 `.env`，不能靠放宽为 777 解决。根 `.env`、服务 `.env`、队列和密钥不进入 Git 或镜像。

## 镜像发布

新增工作流 `Validate and Publish GitHub Webhook Receiver`：PR 验证；main 的推送/手动运行先进行单元与页面回归，再在 amd64、arm64 的 GitHub runner 构建及执行容器验收。公开构建上下文还会用假私有文件验证过滤规则。两平台全部通过后，发布相同已测试镜像字节，不二次构建。

最终镜像为 `ghcr.io/redeployment/xdocker-github-webhook:sha-<完整 XDocker SHA>`。没有自动移动 latest 标签；配置具体 SHA，后续可进一步固定 index digest。当前尚无这个新增镜像的发布记录，不能直接填写一个不存在的标签启动。

接收器镜像可以保持私有。服务器 Docker 用户按既有 [GHCR 登录指导](../ghcr-auth/README.md) 配置 `read:packages` 读取权限；`sudo docker login` 的凭据与普通 `docker login` 分开。Webhook Secret 是另一套密钥，与 PAT、镜像拉取认证、Admin 登录令牌均无关系。

## 服务器配置顺序

先把服务器代码同步到获批、已发布的 XDocker SHA，核对 Admin 也包含 `github_webhooks.py`，再进行以下操作。已确认专用域名为 `hooks.artisan-cloud.com`，站点模板及默认证书清单已登记该域名；不要复用 Admin 私有入口。

1. 初始化配置与目录：

   ```sh
   cd ~/workspace/XDocker
   sh scripts/hosting.sh init
   sh scripts/github-webhook.sh init
   ```

   该初始化只写本机私有目录与 UID/GID，不生成密钥、不联网、不启用服务。已有 `.env` 不被覆盖。

2. 在 `services/github-webhook/.env` 填写已发布的 `GITHUB_WEBHOOK_IMAGE=...:sha-<完整SHA>`；在 `sites/github-webhook/.env` 填写 `SITE_DOMAIN=hooks.artisan-cloud.com`。根 `.env` 的 `ENABLED_SERVICES` 追加 `github-webhook`，`ENABLED_SITES` 追加 `github-webhook`，保留原有项目。

3. 为六个仓库分别生成密钥：

   ```sh
   sh scripts/github-webhook.sh secret PowerX
   sh scripts/github-webhook.sh secret PowerXPlugin
   sh scripts/github-webhook.sh secret PowerXPluginSCRM
   sh scripts/github-webhook.sh secret PowerXPluginECommerce
   sh scripts/github-webhook.sh secret PowerXPluginMediaXStudio
   sh scripts/github-webhook.sh secret PowerXPluginCRM
   ```

   同名密钥存在时保留，不自动轮换。复制某项目的密钥时，在自己的私有终端运行 `sh scripts/github-webhook.sh secret-show PowerX`。不要粘贴到聊天、截图、Git 或工作流日志。缺失或不安全的密钥返回 503，不按无签名模式接收。

4. 部署公网 IP 统一配置在根 `.env` 的 `PREFLIGHT_EXPECTED_IPS`，公开 `.env.example` 默认留空，真实地址只填写在不提交 Git 的私有 `.env` 中。换服务器时修改私有配置里的这项（IPv4 在前，实际使用的 IPv6 在后，以空格分隔），并将域名 A/AAAA 记录同步到对应地址；不在脚本或 Nginx 中写死 IP。已有 `.env` 初始化时不会被覆盖，需要核对并填写这一项。

   ```sh
   # 在部署机器的 XDocker 根目录读取已配置的首个公网地址。
   . ./.env
   xdocker_server_ip=${PREFLIGHT_EXPECTED_IPS%% *}
   : "${xdocker_server_ip:?请先在根 .env 配置 PREFLIGHT_EXPECTED_IPS}"
   ```

   该值用于 Admin「部署检查」的 DNS 指向校验，不改变 Nginx 的 `HTTP_BIND/HTTPS_BIND`、容器网络、DNS 或 SSH 配置。Webhook Payload URL 使用域名，不包含 IP。未登记的 AAAA 或旧服务器地址会被 DNS 检查标记为失败；改 IP 后，已有 DNS 结果会标为过期，需重新检查。

   默认 `certbot/certificates.json` **已经包含** `hooks.artisan-cloud.com` 的独立证书条目，`enabled=true`；使用默认清单无需重复添加。登记不表示证书已签发。若根 `.env` 的 `CERTIFICATES_INVENTORY` 指向已有自定义清单，保留所有原有条目，并补齐：

   ```json
   {"name":"hooks.artisan-cloud.com","domains":["hooks.artisan-cloud.com"],"enabled":true}
   ```

   修改域名时同步更新站点 `.env` 的 `SITE_DOMAIN/SITE_CERT_NAME` 与证书条目。私有清单可在当前在用清单的副本上维护，并通过 `CERTIFICATES_INVENTORY` 配置路径；不能用空清单替换现有证书配置。

5. 按既有容器 Nginx/Certbot 流程签发：

   ```sh
   sh scripts/hosting.sh http github-webhook
   sh scripts/hosting.sh issue-test github-webhook
   sh scripts/hosting.sh issue github-webhook
   sh scripts/hosting.sh https github-webhook
   sh scripts/github-webhook.sh status
   ```

   仅 HTTP 阶段开放 ACME 路径；其他 HTTP 请求返回 503，**此时不能配置 GitHub 投递**。HTTPS 后只有 `/github/webhook/` 的 POST 转发到接收器；`/healthz` 与 `/api/` 不公开。续期继续使用共用 certbot-renew，不另装主机 Certbot。

6. 手动导入默认可用；若需要持续接收通知，在 `services/admin/.env` 设置 `ADMIN_GITHUB_WEBHOOK_ENABLED=true`，按获批部署流程更新 Admin。启动后每 5 秒读取最多 50 条队列事件，**不请求 GitHub API**。`ADMIN_GITHUB_WATCH_ENABLED` 仍是另一项独立、默认关闭的 API 轮询开关。两个开关都关闭时，Webhook 仍可入队，网页点击“导入已接收事件”导入。

新 Admin 增加了队列挂载，升级该容器前应先初始化队列目录；已有 `scripts/admin.sh init` 也会创建此目录。只启用接收器而不更新 Admin，不会自动出现研发通知。

## GitHub 每个仓库怎样填写

由仓库维护者进入仓库 `Settings → Webhooks → Add webhook`，选择 Content type `application/json`、SSL verification 开启、Active 开启。各仓库使用各自的 Secret 和如下 Payload URL：

| 仓库 | URL 路径 |
| --- | --- |
| ArtisanCloud/PowerX | `/github/webhook/PowerX` |
| ArtisanCloud/PowerXPlugin | `/github/webhook/PowerXPlugin` |
| ArtisanCloud/PowerXPluginSCRM | `/github/webhook/PowerXPluginSCRM` |
| ArtisanCloud/PowerXPluginECommerce | `/github/webhook/PowerXPluginECommerce` |
| Matrix-X/PowerXPluginMediaXStudio | `/github/webhook/PowerXPluginMediaXStudio` |
| Matrix-X/PowerXPluginCRM | `/github/webhook/PowerXPluginCRM` |

完整例子：`https://hooks.artisan-cloud.com/github/webhook/PowerX`。只配置 repository hooks；组织 hook 的 ping 不作为六项目仓库验收。

选择 “Let me select individual events”：Pushes、Pull requests、Pull request reviews、Issue comments、Pull request review comments、Commit comments、Workflow runs、Workflow jobs、Check runs、Check suites、Commit statuses。接收事件为 `push/pull_request/pull_request_review/issue_comment/pull_request_review_comment/commit_comment/workflow_run/workflow_job/check_run/check_suite/status`；`ping` 也支持。

未知项目 404；错误 Content type/JSON/repository/delivery 400；缺失或错误 SHA256 签名 401；未知事件 422；同 delivery 内容冲突 409。正文上限 **2 MiB**（低于 GitHub 的最大 payload）；超限 413，不能当作接收成功。队列满、被锁、不可写或密钥不可读返回 503；Nginx 超速 429。只有提交到持久队列后才返回 202；已确认相同 delivery 的重投返回 200。

## 网页使用与失败重投

进入私有 XDocker 管理网页 → “研发事件”：

1. “Webhook 接收队列”查看队列可读状态、待导入数量、最近接收/导入时间、导入开关与错误。GET 读取缓存不导入、不向 GitHub 发请求。
2. 默认手动模式点击“导入已接收事件（最多 50 条）”，直到待导入数归零。已启用自动导入时等待一个周期。
3. 收件箱标注“已验签 Webhook”、event/action、delivery、SHA、attempt 和评论/失败步骤。文本转义显示；已读只是本地回执，不解决 CI/审查问题。
4. 新事件把对应项目标记为需要重新采集，保留旧快照；导入时更新观察版本，阻止较早采集任务覆盖为当前通过。乱序、旧 SHA、取消、重跑成功、删除评论均只作为事件证据，不直接覆盖当前 CI 快照。点击项目“采集”重新读取 GitHub 的当前提交/检查；私有仓库需对应 owner 的只读 API token。逐项目“观察启用”只控制 API 采集，签名事件仍保留在收件箱。

GitHub Recent deliveries 中应看到首次 202、相同 delivery 重投 200。接收器不会自动调用 GitHub 写 API；GitHub 失败投递也**不会自动重投**。修复域名/证书/密钥/队列问题后，维护者在 GitHub 的 delivery 详情点击 Redeliver。后台导入失败的事件仍在本地队列；恢复读写后网页再次导入或等待自动导入，不需要 GitHub 重新发送。

队列事件只保存必要字段和有限文本，凭据模式脱敏，不保存原始 payload/签名/Secret。delivery 持久去重，重启后仍有效；通知提交后才删除待导入 payload，途中中断可安全重试。接收凭据最多 100000 条、待导入最多 10000 条，当前不自动删除 delivery 凭据；达到上限返回 503。Admin 的导入凭据也上限 100000，通知总量上限 10000，沿用已读旧通知清理与未读保护。归档/增加容量需单独维护方案，不能直接删库“解决”。

需要在停止接收器与 Admin 导入后，同批次备份/恢复两个 SQLite 数据库及私有 webhook secrets；分别在线执行 SQLite backup 不保证跨库一致。备份包含私有评论，必须限制访问。未知 schema/损坏/符号链接不自动重建；先留存原文件、确认原因。无自动 GitHub redelivery、完整原始事件审计或邮件/Slack 通知。

官方依据：[签名验证](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)、[及时确认与队列/去重](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks)、[事件与 payload](https://docs.github.com/en/webhooks/webhook-events-and-payloads)、[失败投递手工重投](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks)。
