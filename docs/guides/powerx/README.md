# 用 XDocker 部署 PowerX 开发实例

本指南面向部署用户。PowerX 前后端使用已发布镜像，服务器不编译 Go/Nuxt。只部署 PowerX、不使用 XDocker 的用户，使用 PowerX Release 附带的 `powerx-docker.tar.gz`，按其中的 README 操作；两种方式使用同一份镜像。

当前目标是全新 `powerx-dev` 实例，域名 `powerx-dev.artisan-cloud.com`。本流程不迁移旧服务器数据库，也不创建正式 `powerx` 环境。

当前固定版本来自远程 **develop**，源码 SHA `2f28c9f830e0364d2383bfb0309df2ccbe98727a`，默认第一次启动保持未安装状态并进入 Setup。镜像通过[完整安装向导验收](https://github.com/ArtisanCloud/PowerX/actions/runs/37795862441)。独立用户可获取[镜像部署包](https://github.com/ArtisanCloud/PowerX/releases/tag/docker-dev-2f28c9f830e0)，无需克隆源码。

## 结构与隔离

```text
apps/powerx/compose.yml          可复用应用模板，无 build 指令
instances/powerx-dev/.env        该实例镜像、浏览器地址、环境与管理员邮箱
instances/powerx-dev/config/     私有数据库密码、密钥、账号与初始化标记
instances/powerx-dev/instance.json  管理控制台的实例登记
data/instances/powerx-dev/
  postgres/                     独立 PostgreSQL，带 pgvector
  redis/                        独立 Redis AOF
  runtime/                      上传、插件、日志、运行状态
sites/powerx-dev/                域名及共享入口网络
```

独立 Compose 项目 `powerx-dev`：postgres、redis、backend、web-admin。数据库与缓存仅加入实例私有网络，不发布宿主机端口。共享 Nginx 加入 `powerx-dev_ingress` 网络，通过专用别名连接前后端。未来正式环境使用另一套项目、网络、配置和数据目录。

## 1. 准备服务器

按 [服务器准备指南](../server-setup/README.md) 完成 Ubuntu 用户、SSH 和 Docker。确认：

```bash
docker version
docker compose version
```

没有 docker 组权限时使用 sudo；加入组后必须重新 SSH 登录。使用 sudo 拉取镜像时也使用 sudo 登录仓库。

## 2. 选择已验证镜像

PowerX 仓库的 `Publish PowerX Container Distribution` 工作流会发布：

```text
ghcr.io/artisancloud/powerx-backend:sha-<完整SHA>
ghcr.io/artisancloud/powerx-web-admin:sha-<同一完整SHA>
```

选择**完整成功**的工作流版本：它验证全新数据库迁移、初始化、前端访问与管理员登录。不能仅根据后端构建成功就部署；不要混用不同 SHA 的前后端。

镜像 Public 时不需要账号。GHCR 默认可见性与公开 GitHub 源码仓库不同；发布者须单独设置两个包为 Public，才可以让普通用户匿名下载。保留 Private 时，按 [私有镜像认证](../ghcr-auth/README.md) 使用账号包权限和 PAT classic `read:packages`。

## 3. 同步 XDocker 并初始化实例模板

```bash
cd ~/workspace/XDocker
git pull --ff-only
git rev-parse HEAD
sh scripts/hosting.sh init
sudo sh scripts/apps.sh init powerx-dev
```

`apps.sh init` 只准备实例运行配置和管理登记，不重置或创建数据库。编辑 `instances/powerx-dev/.env`：

```dotenv
COMPOSE_PROJECT_NAME=powerx-dev
DEPLOYMENT_ENV=dev
POWERX_BACKEND_IMAGE=ghcr.io/artisancloud/powerx-backend:sha-<已验证SHA>
POWERX_WEB_IMAGE=ghcr.io/artisancloud/powerx-web-admin:sha-<同一SHA>
POSTGRES_IMAGE=pgvector/pgvector:pg16
REDIS_IMAGE=redis:7-alpine
PULL_POLICY=missing
PUBLIC_ORIGIN=https://powerx-dev.artisan-cloud.com
PUBLIC_WS_ORIGIN=wss://powerx-dev.artisan-cloud.com
```

管理员账号、邮箱和密码在 Setup 页面由部署用户填写，启动脚本不代填。`PUBLIC_ORIGIN` 是浏览器入口，`PUBLIC_WS_ORIGIN` 使用相同域名的 wss；不能填 `backend:8080` 或服务器内网地址。容器内部 HTTP 固定 8080、Web 固定 3000，与实例环境名无关。

镜像已下载/安全导入时可设 `PULL_POLICY=never`；缺少任何配置的镜像则启动失败。这里的策略同时作用于应用、PostgreSQL 和 Redis。

```bash
sudo sh scripts/apps.sh compose powerx-dev config --quiet
sudo sh scripts/apps.sh compose powerx-dev pull postgres redis backend web-admin init
```

## 4. 启动应用

```bash
sudo sh scripts/apps.sh start powerx-dev
sudo sh scripts/apps.sh status powerx-dev
```

普通启动只生成 Docker 基础私有配置、启动数据库/缓存和前后端。第一次配置状态为 uninstalled，应用数据库没有迁移、种子和管理员；前端应进入 `/setup`。完成 Setup 后，后端经 Docker 重启加载完整运行状态。

已有已安装配置会保留当前账号、数据和密钥，不通过升级自动重置为 Setup。若已初始化实例需要重新体验首次安装，必须先备份/验证，再明确选择全新目录；不得仅把 installed 标志改为 uninstalled 并复用旧数据库。

## 5. 接入域名和 HTTPS

先把 A 记录指向新服务器；存在 AAAA 时必须同步配置到可用 IPv6 或移除无效记录。编辑 `sites/powerx-dev/.env` 的 `SITE_DOMAIN`，并在根 `.env` 的 `ENABLED_SITES` 中追加 `powerx-dev`，保留现有静态站与 FRP。

在当前证书清单中启用 `powerx-dev.artisan-cloud.com`；若使用私有清单，编辑 `certbot/certificates.local.json`，而不是覆盖其他域名记录。邮箱沿用根 `.env` 的 CERTBOT_EMAIL。

```bash
sudo sh scripts/hosting.sh http powerx-dev --no-pull
sudo sh scripts/hosting.sh issue-test powerx-dev
sudo sh scripts/hosting.sh issue powerx-dev
sudo sh scripts/hosting.sh https powerx-dev
sudo sh scripts/hosting.sh renew-test powerx-dev
sudo sh scripts/certificates.sh check --report /var/lib/certbot-events/certificates-check.json
```

最后的 check 将真实证书检查结果保存到管理界面读取的报告中；未生成报告时界面显示“待检查”，不代表证书不存在。HTTP 接入可能重新创建 Nginx 以连接实例 ingress 网络，安排好对其他网站的短暂影响。签发失败先保留 HTTP 和应用数据，按证书工具的具体失败原因检查公网 80、DNS 与 ACME 网络，不反复强制签发。

入口把页面代理到 Web Admin，`/api/`、`/ws/`、`/media/` 和 healthz 代理到后端，保留路径、Host 和 X-Forwarded-Proto；支持 WebSocket Upgrade、SSE 和长连接。部署脚本不安装宿主机 Nginx 或 Certbot。

## 6. 登录和验收

打开 **https://powerx-dev.artisan-cloud.com/setup**。在自己的服务器终端读取数据库与 Redis 的 Docker 连接值：

```bash
sudo sh scripts/apps.sh setup-values powerx-dev
```

数据库选择 PostgreSQL，主机 `postgres`、端口 `5432`、数据库和用户均为 `powerx`，密码使用输出 database.password；Redis 主机 `redis`、端口 `6379`、密码使用 cache.password。本地存储路径 `/data/uploads`，公开地址为域名加 `/media`。部署环境 dev，内部端口保持后端 8080、Web 3000；HTTPS 由 XDocker 管理，不在 PowerX 再签发。

按页面顺序测试连接、保存和初始化，管理员账号、邮箱、密码由你设置，最后完成安装。脚本不会自动创建管理员或跳过向导。数据库迁移和种子只在用户明确点击初始化时执行；Setup 完成后进入登录，并使用自己设置的账号。

```bash
curl -fsS https://powerx-dev.artisan-cloud.com/api/v1/health
sudo sh scripts/apps.sh logs powerx-dev
```

检查已安装状态、用户与租户信息、页面刷新、浏览器 API/WebSocket/SSE。AI 模型、知识检索和具体插件需另配真实服务、权限与对应 Linux 架构制品，并分别验收；HTTP 200 不代表这些业务功能已经可用。

XDocker 专属控制台根据 `instance.json` 明确登记的项目显示四个应用容器，名称为 `powerx-dev/backend` 等；启停需输入完整名称。其他 Compose 项目不会因存在 Docker socket 就自动获得管理权限。新增实例后按管理面板指南同步并启动最新 Admin 镜像。

## 7. 升级、备份和回滚

备份实例 config、数据库和 runtime；数据库应使用 pg_dump/一致性备份，不能在运行时只复制 postgres 数据目录。加密和 JWT 密钥也须备份，丢失后不能简单生成另一套来替代。

更新前后端为同一已验证 SHA，先拉取，再按版本说明迁移并启动：

```bash
sudo sh scripts/apps.sh compose powerx-dev pull backend web-admin init
sudo sh scripts/apps.sh migrate powerx-dev
sudo sh scripts/apps.sh start powerx-dev
```

升级不重新 init、不调用 refresh。数据库迁移不兼容时，镜像回滚必须配合对应数据备份恢复。

```bash
sudo sh scripts/apps.sh stop powerx-dev     # 只停该实例，保留数据
sudo sh scripts/apps.sh start powerx-dev
```

不要对共享入口执行整个 XDocker 的 down 或 remove-orphans，也不要把实例 config/数据目录拷贝给其他部署用户作为模板。用户收到的应是公开模板与固定镜像版本，每个部署自行生成私有配置。

## 旧自动初始化流程的历史验收（已纠正）

2026-10-08，在 `160.202.238.184` 部署 develop 固定 SHA `0e1c4a561b3ba997722d1724049671390120b184`：四个应用容器健康；公网 HTTPS 页面、健康 API、真实管理员登录及 `/api/v1/admin/user/auth/me/context` 返回 200。前端运行配置使用正确 HTTPS/WSS 域名，PostgreSQL、Redis、前后端均不发布宿主机端口。

正式证书有效至 `2027-01-06T12:39:56+00:00`，签发测试与模拟续期通过，加入现有自动续期调度。专属控制台支持这四个登记服务及动态站点；原有三个静态网站、五条 FRP 健康入口均仍返回 HTTPS 200。除共享 Nginx 为新网络做了一次重建，其余既有容器保持 ID 和启动时间。

XDocker 68 项回归通过，PowerX 的独立部署包在 CI 验证空库初始化、页面与管理员登录，并发布双架构镜像；匿名镜像访问验证通过。本机 Docker daemon 未启动，运行验证由 CI 和目标服务器完成。浏览器自动化当前不可用，浏览器实际交互、模型调用和具体插件安装没有据此标记为已验收。

## 备份管理

XDocker 的“实例备份”支持维护窗口下的 PostgreSQL 逻辑导出、Redis RDB、配置/密钥和文件组合打包，私有下载、校验、隔离恢复演练和显式保留清理。它独立于 PowerX 应用内的数据库备份策略。详见 [实例备份与应用备份](backups.md)。
