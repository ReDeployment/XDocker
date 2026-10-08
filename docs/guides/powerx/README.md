# 用 XDocker 部署 PowerX 开发实例

本指南面向部署用户。PowerX 前后端使用已发布镜像，服务器不编译 Go/Nuxt。只部署 PowerX、不使用 XDocker 的用户，使用 PowerX Release 附带的 `powerx-docker.tar.gz`，按其中的 README 操作；两种方式使用同一份镜像。

当前目标是全新 `powerx-dev` 实例，域名 `powerx-dev.artisan-cloud.com`。本流程不迁移旧服务器数据库，也不创建正式 `powerx` 环境。

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
ADMIN_USERNAME=admin
ADMIN_EMAIL=your-email@example.com
```

管理员邮箱由部署用户填写。`PUBLIC_ORIGIN` 是浏览器入口，`PUBLIC_WS_ORIGIN` 使用相同域名的 wss；不能填 `backend:8080` 或服务器内网地址。容器内部 HTTP 固定 8080、Web 固定 3000，与实例环境名无关。

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

流程依次生成私有配置、启动并等待数据库/缓存健康、对新数据库执行 migrate 和 seed、保存初始化标记、启动前后端并等待健康。

普通启动不会重新生成密钥，不重复 seed，不执行 refresh，也不清空数据。首次管理员密码独立随机生成。容器部署不创建 PowerX 公共本地开发 API keys；插件应通过后台创建和授权自己的 API key。

若首次初始化部分失败，保留日志和目录，先查明阶段。不要删除 config、数据库或初始化标记来重试。重复启动已初始化实例会保留当前账号密码和数据。

## 5. 接入域名和 HTTPS

先把 A 记录指向新服务器；存在 AAAA 时必须同步配置到可用 IPv6 或移除无效记录。编辑 `sites/powerx-dev/.env` 的 `SITE_DOMAIN`，并在根 `.env` 的 `ENABLED_SITES` 中追加 `powerx-dev`，保留现有静态站与 FRP。

在当前证书清单中启用 `powerx-dev.artisan-cloud.com`；若使用私有清单，编辑 `certbot/certificates.local.json`，而不是覆盖其他域名记录。邮箱沿用根 `.env` 的 CERTBOT_EMAIL。

```bash
sudo sh scripts/hosting.sh http powerx-dev --no-pull
sudo sh scripts/hosting.sh issue-test powerx-dev
sudo sh scripts/hosting.sh issue powerx-dev
sudo sh scripts/hosting.sh https powerx-dev
sudo sh scripts/hosting.sh renew-test powerx-dev
```

HTTP 接入可能重新创建 Nginx 以连接实例 ingress 网络，安排好对其他网站的短暂影响。签发失败先保留 HTTP 和应用数据，按证书工具的具体失败原因检查公网 80、DNS 与 ACME 网络，不反复强制签发。

入口把页面代理到 Web Admin，`/api/`、`/ws/`、`/media/` 和 healthz 代理到后端，保留路径、Host 和 X-Forwarded-Proto；支持 WebSocket Upgrade、SSE 和长连接。部署脚本不安装宿主机 Nginx 或 Certbot。

## 6. 登录和验收

在自己的服务器终端读取初始账号：

```bash
sudo sh scripts/apps.sh credentials powerx-dev
```

使用输出中的邮箱和随机密码登录 **https://powerx-dev.artisan-cloud.com**。登录后修改初始密码。输出及 `initial-admin.json` 是私有凭据，不发到聊天、不分享截图、不提交 Git。

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

升级不重新 init、不删除 initialized 标记、不调用 refresh。数据库迁移不兼容时，镜像回滚必须配合对应数据备份恢复。

```bash
sudo sh scripts/apps.sh stop powerx-dev     # 只停该实例，保留数据
sudo sh scripts/apps.sh start powerx-dev
```

不要对共享入口执行整个 XDocker 的 down 或 remove-orphans，也不要把实例 config/数据目录拷贝给其他部署用户作为模板。用户收到的应是公开模板与固定镜像版本，每个部署自行生成私有配置。
