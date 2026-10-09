# 用 XDocker 部署 PowerX 开发实例

本指南面向部署用户。PowerX 前后端使用已发布镜像，服务器不编译 Go/Nuxt。只部署 PowerX、不使用 XDocker 的用户，使用 PowerX Release 附带的 `powerx-docker.tar.gz`，按其中的 README 操作；两种方式使用同一份镜像。

当前目标是全新 `powerx-dev` 实例，域名 `powerx-dev.artisan-cloud.com`。本流程不迁移旧服务器数据库，也不创建正式 `powerx` 环境。

当前固定版本来自远程 **develop**，源码 SHA `3d792fd7ea2426825970bceceb0d6e9db46211ef`，默认第一次启动保持未安装状态并进入 Setup。镜像通过[完整安装向导验收](https://github.com/ArtisanCloud/PowerX/actions/runs/37932655764)。独立用户可获取[镜像部署包](https://github.com/ArtisanCloud/PowerX/releases/tag/docker-dev-3d792fd7ea24)，无需克隆源码。

## 结构与隔离

```text
apps/powerx/compose.yml          可复用应用模板，无 build 指令
instances/powerx-dev/.env        该实例镜像、浏览器地址与环境
instances/powerx-dev/config/     私有数据库密码、密钥与安装配置
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

也可以直接在 XDocker 的“PowerX 实例”页面读取连接值并显示/复制密码，操作见[实例管理页面指南](instances.md)。

打开 **https://powerx-dev.artisan-cloud.com/setup**。在自己的服务器终端读取数据库与 Redis 的 Docker 连接值：

```bash
sudo sh scripts/apps.sh setup-values powerx-dev
```

### 初始数据库密码在哪里

第一次 `apps.sh start` 会为每个实例随机生成 PostgreSQL 和 Redis 密码，没有通用默认密码。这些密码不在公开镜像或 `.env.example` 中，也不等于 Linux 的 root/ubuntu 密码、PowerX 管理员密码或 XDocker 管理令牌。

- PostgreSQL 密码文件：`instances/powerx-dev/config/postgres-password`，同时写入私有 `config.yaml` 的 `database.password`。
- Redis 密码文件：`instances/powerx-dev/config/redis-password`，同时写入 `config.yaml` 的 `cache.password`。
- 文件权限 600。上面的 `setup-values` 输出 JSON，数据库密码取 `database.password`，Redis 密码取 `cache.password`；仅在自己的服务器终端查看，不发到聊天或提交 Git。

在 Setup 的“数据库 & 基础配置”按以下值填写。连接测试由后端容器执行，主机地址使用 Docker 服务名：

| 页面字段 | 值或来源 |
| --- | --- |
| 数据库类型 | `postgresql` |
| 数据库版本 | `16`（可留空，不填示例中的 MySQL 8.0） |
| 主机地址 | `postgres` |
| 端口 | `5432` |
| 数据库名 | `powerx` |
| 用户名 | `powerx` |
| 密码 | `setup-values` 输出的 `database.password` |
| 字符集 | `utf8`；PostgreSQL 的连接逻辑不使用 MySQL 的 charset 参数 |
| 缓存类型 | `redis` |
| Redis 主机 / 端口 | `redis` / `6379` |
| Redis 密码 | 输出的 `cache.password` |
| Redis 数据库索引 | `0` |

将页面默认的 `localhost` 和 `root` 改为上表值。填写后点击“测试数据库连接”，等待成功；Redis 测试成功后再继续保存和初始化。连接失败时检查主机、用户和密码，不重建数据库或改用服务器公网 IP。

本地存储路径 `/data/uploads`，公开地址为域名加 `/media`。部署环境 dev，内部端口保持后端 8080、Web 3000；HTTPS 由 XDocker 管理，不在 PowerX 再签发。

按页面顺序测试连接、保存和初始化，管理员账号、邮箱、密码由你设置，最后完成安装。脚本不会自动创建管理员或跳过向导。数据库配置步骤只执行表结构迁移；管理员信息确认后的“完成安装”执行完整种子初始化。Setup 完成后进入登录，并使用自己设置的账号。

```bash
curl -fsS https://powerx-dev.artisan-cloud.com/api/v1/health
sudo sh scripts/apps.sh logs powerx-dev
```

检查已安装状态、用户与租户信息、页面刷新、浏览器 API/WebSocket/SSE。AI 模型、知识检索和具体插件需另配真实服务、权限与对应 Linux 架构制品，并分别验收；HTTP 200 不代表这些业务功能已经可用。

XDocker 专属控制台根据 `instance.json` 明确登记的项目显示四个应用容器，名称为 `powerx-dev/backend` 等；启停需输入完整名称。其他 Compose 项目不会因存在 Docker socket 就自动获得管理权限。新增实例后按管理面板指南同步并启动最新 Admin 镜像。

### 如何修改 PostgreSQL 密码

只在 Setup 中输入新密码不会修改数据库账号。`POSTGRES_PASSWORD_FILE` 只在空数据目录首次初始化时设置账号密码；已有数据库必须先修改 PostgreSQL 角色，再同步 PowerX 私有配置。[官方镜像说明](https://hub.docker.com/_/postgres)。

以下步骤由服务器管理员执行，密码由你自己选择；不会重置数据库、管理员、密钥或安装状态。先在 XDocker“实例备份”创建并验证一份备份，再暂停该实例前后端，数据库与 Redis 保持运行：

```bash
cd ~/workspace/XDocker
git pull --ff-only
sudo sh scripts/apps.sh compose powerx-dev stop backend web-admin
sudo sh scripts/apps.sh compose powerx-dev exec postgres psql -X -U powerx -d powerx
```

在 psql 提示符输入：

```text
\password powerx
\q
```

`\password` 会要求输入两次新密码，输入不回显；没有错误并返回 psql 提示符后退出，下一步会再用新密码验证真实连接。该命令避免明文密码进入 SQL 命令历史和服务器日志。[psql 文档](https://www.postgresql.org/docs/16/app-psql.html)。

然后将**同一个新密码**同步到私有配置，命令不会把密码放入参数或打印出来：

```bash
sudo sh scripts/apps.sh compose powerx-dev run --rm --no-deps \
  -v "$PWD/scripts/powerx-db-config.py:/tools/powerx-db-config.py:ro" \
  init python3 /tools/powerx-db-config.py
```

再次按提示输入两遍新密码。工具先用新密码执行真实 TCP `SELECT 1`；通过后更新 `config.yaml` 的 `database.password`、其中已有的 PostgreSQL URI DSN，以及 `postgres-password`。匹配该数据库的 Setup 草稿也会同步，原文件保存在私有 `config/password-change-backups/` 中。不同数据库的草稿不会被改写。自定义 DSN 与字段不一致时拒绝同步，须先检查配置；验证失败时文件保持原样。

看到 `TCP authentication passed` 和 `synchronized` 后才恢复服务：

```bash
sudo sh scripts/apps.sh compose powerx-dev up -d --wait backend web-admin
sudo sh scripts/apps.sh status powerx-dev
```

未完成 Setup 时，刷新向导并把数据库密码填为新值，再测试连接。已安装时验证应用健康及登录，并生成新的备份。旧备份仍携带旧密码，恢复时数据库角色与配置须对应。

任一步失败，保持前后端暂停，不执行 reset、删除 postgres 目录或只改 `.env`。重新用 `\password powerx` 设置你确认的密码，再运行同步工具；若要回退，使用改密前密码重复上述两个步骤。此流程仅修改 PostgreSQL，不修改 Redis 密码。

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

## 开发实例已切换为首次安装

2026-10-09，`powerx-dev` 已实际切换到前后端 SHA `2f28c9f830e0364d2383bfb0309df2ccbe98727a`，使用全新配置与数据目录。公网 `/api/v1/admin/setup/status` 返回 `install_status=uninstalled`、`configured=false`、`requires_login=false`，用户与租户均为 0；PostgreSQL 的 public 应用表数为 0。四个容器健康，未替用户提交 Setup 或创建管理员。

访问根地址或原登录地址时，浏览器路由根据此状态进入 Setup；也可直接打开 `/setup`。前端为客户端渲染，HTTP 200 的页面壳本身不表示已安装或已登录。页面仍停留在旧登录画面时先刷新，使浏览器重新读取安装状态。本次已验证公网状态接口、页面与资源响应；浏览器自动化不可用，实际点击向导尚未代替用户执行。

切换前备份 `6be868b9eba2997ce6ea99c6d827ad91` 已完成隔离恢复验证。旧配置、数据及实例环境配置原样保存在服务器私有目录 `data/preserved-instances/powerx-dev-20261009T015715Z/`（权限 700），没有删除或覆盖旧数据库。其他网站、FRP、入口和管理服务保持原容器 ID 与启动时间。


## Setup 数据库步骤的管理员依赖错误

旧镜像在数据库步骤执行完整 seed，但该步骤尚未收集管理员密码，可能出现 `seed_native_marketing_skills_root_user_missing`。修复版将数据库步骤限定为迁移表结构，完整种子延迟到管理员信息确认后的完成安装；迁移重试不会自动创建默认管理员。最新 develop 的能力目录还含超过 128 字符的 ID，相关存储字段已扩展，避免完成阶段再次报长度错误。

修复镜像 SHA `3d792fd7ea2426825970bceceb0d6e9db46211ef` 的完整工作流验证了：首次空库进入 Setup、未填写管理员密码的数据库步骤及重试、管理员确认后的完整种子、长能力 ID 写入、后端重启与登录。更新前先备份当前部分初始化的数据；升级保留 config、Setup 草稿和数据库，不执行 refresh 或重置。

数据库主机使用 `postgres`，Redis 主机使用 `redis`；两项密码分别从 XDocker 实例对应标签复制。Redis 容器启用了认证，向导中的“可选”占位文案不表示本实例可以留空。刷新向导并重试数据库步骤，通过后继续设置自己的管理员账号和密码。

2026-10-09，服务器已部署上述固定 SHA 的前后端。更新前备份 `c6d57e2b30c08970dbbcaf9551718147` 完成隔离恢复验证；更新后使用现有草稿调用数据库初始化接口成功，实际用户表仍为 0，能力 ID 字段长度为 256。只替换前后端容器，数据库、Redis、Nginx、FRPS 和管理服务的容器 ID/启动时间保留；未自动完成用户的安装向导。Setup 包及相关模型的测试、vet 与真实容器完整安装/登录验收通过；全仓库测试和 vet 仍有既有媒体/EventFabric/技能夹具等失败，不据此声称全仓库全绿。
