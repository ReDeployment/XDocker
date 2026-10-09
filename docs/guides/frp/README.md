# FRP 穿透服务与多域名入口

服务器运行一个独立 `frps` 容器，本地运行 `frpc`，多个业务域名共用 FRPS。固定版本为 0.52.3，与现有本地 ARM64 客户端及旧服务器一致；版本升级单独验证。

```text
浏览器 HTTPS → Nginx → frps:8080 → TLS 隧道 → 本地 frpc → 本地业务服务
本地 frpc 主动连接服务器 7000；8080 仅供 Docker 网络内 Nginx 使用。
```

## 1. 文件与私有配置

- `services/frps/`：镜像构建、Compose 和环境模板；镜像无生产 token 或私钥。
- `clients/frpc/main.ini.example`：6 个业务映射模板，默认对应本地 8091、8092、8077、8078、8110、8111。
- `sites/debug-ecommerce`、`debug-scrm`、`debug-powerx-local`、`debug-powerxplugin-local`、`shopify`、`court-mate-api-dev`：域名与证书组，类型为 `frp_http`，无需独立站点镜像。
- `data/frp/`：真实 token、传输证书、私钥、客户端配置和日志，整个目录被 Git 忽略。
- `scripts/frp.sh`：初始化、检查、启动和本地客户端生命周期；不自动修改旧客户端配置。

FRPS 启用 token 和强制 TLS。客户端使用通过可信 SSH 下载的 `server.crt` 验证服务端身份，不只启用加密。传输证书是专用自签证书，与浏览器 HTTPS 的 Let's Encrypt 证书独立；不会让浏览器信任自签证书。初始化证书有效期为 365 天，需在到期前协调更新服务端证书及客户端信任文件，保留私有配置备份。

## 2. 发布与服务器启动

所有代码先本地修改、验证并推送，再让服务器同步相同提交。`.github/workflows/publish-frps.yml` 发布 `ghcr.io/redeployment/xdocker-frps:0.52.3` 和完整 SHA 标签，包含 AMD64、ARM64。构建下载官方发行文件并核对 [官方校验和](https://github.com/fatedier/frp/releases/download/v0.52.3/frp_sha256_checksums.txt)。查看 [镜像包页面](https://github.com/orgs/ReDeployment/packages/container/package/xdocker-frps) 和 Actions 运行检查；Private 包按 [GHCR 认证指南](../ghcr-auth/README.md) 登录。

外部部署者没有我们私有包的权限时，可以直接从本公开仓库执行 `docker build -t xdocker-frps:0.52.3 services/frps`，将自己的 `FRPS_IMAGE` 指向本地镜像；仍使用相同官方文件和固定校验和。

```bash
cd ~/workspace/XDocker
git pull --ff-only
sh scripts/hosting.sh init
sh scripts/frp.sh init
```

根 `.env` 设置 `ENABLED_SERVICES="frps"`，保留当前 `ENABLED_SITES`。`services/frps/.env` 设置真实镜像标签和控制端口，镜像完整下载或导入后可设 `FRPS_PULL_POLICY=never`。

```bash
sudo sh scripts/frp.sh verify
sudo sh scripts/frp.sh start
sudo sh scripts/frp.sh status
sh scripts/frp.sh client-config 160.202.238.184
```

`init` 保留已经存在的 token、密钥和配置；`client-config` 不覆盖现有客户端文件。真实配置和 token 不输出到终端，也不放进命令参数。仅开放所选控制端口 7000，保留现有网站 80/443；不发布 8080 或管理面板。

## 3. 本地客户端并行验证

通过可信 SSH 私下传输服务器生成的 `data/frp/frpc.ini` 和 `data/frp/server.crt` 到本机 XDocker 的 `data/frp/`，目录 700、配置 600。不需要传输服务器私钥。客户端模板的可信证书路径相对 XDocker 根目录；包装脚本会先进入根目录。

```bash
FRPC_BIN=/private/var/www/html/ArtisanCloud/dev/frp/frp_0.52.3_darwin_arm64/frpc \
  sh scripts/frp.sh start-client
```

原来的 FRPC 可以继续连接旧服务器，新客户端连接新服务器；相同本地业务端口可供两边验证。托管进程的 PID 与私有日志保存在 `data/frp/`。停止仅针对此脚本创建的客户端：

```bash
sh scripts/frp.sh stop-client
```

macOS 的 `start-client` 使用当前用户的 LaunchAgent，在关闭终端后持续运行，并在用户登录后自动启动；首次创建和控制 LaunchAgent 的辅助脚本需要本机 Python 3。其他系统暂使用 nohup，并行验证后应配置系统服务。FRPC 0.52.3 登录失败可能仍返回退出码 0，必须核对进程、登录/注册日志及真实业务请求。根路径 404 对 API 服务可能正常，应使用实际健康接口、API 或 WebSocket 做验收。

## 4. 域名与证书迁移

根 `.env` 把所需 FRP 站点加入 `ENABLED_SITES`，保留三个静态站点。`services/frps/.env` 的 `FRPS_SUBDOMAIN_HOST` 必须与客户端子域名后缀一致。

```bash
sudo sh scripts/hosting.sh http debug-ecommerce --no-pull
sudo sh scripts/hosting.sh http debug-scrm --no-pull
sudo sh scripts/hosting.sh http debug-powerx-local --no-pull
sudo sh scripts/hosting.sh http debug-powerxplugin-local --no-pull
```

外部使用 `curl --resolve debug-ecommerce.artisan-cloud.com:80:160.202.238.184 http://debug-ecommerce.artisan-cloud.com/实际健康路径` 检查新入口，再切换域名。Nginx 保留 Host、转发协议头、WebSocket Upgrade；关闭响应缓冲以支持 SSE。

当前 debug 三域名证书组名为 `debug-powerxplugin-local.artisan-cloud.com`，SAN 包含 ecommerce、scrm 和新的插件域名。已有旧证书组不能直接修改 SAN 后继续续期；本次以新 lineage 签发，再切换各站点的 `SITE_CERT_NAME`，保留旧证书用于回退。独立的 `debug-powerx-local` 使用自己的证书项，Shopify 也使用独立项。

`court-mate-api-dev.artisan-cloud.com` 不在旧 debug SAN 中，需在本机证书清单增加独立项：

```json
{"name":"court-mate-api-dev.artisan-cloud.com","domains":["court-mate-api-dev.artisan-cloud.com"],"enabled":true}
```

企业微信验证文件放在 `data/acme/verification/WW_verify_<标识>.txt`，由 Nginx 直接提供，不依赖本地业务在线。真实文件从旧服务器核对后迁移；旧隧道和 DNS 在新链路验收前保留。

## 5. 验收与回滚边界

```bash
python3 -m unittest discover -s tests -v
python3 tests/frp_smoke.py --frps /path/to/frps --frpc /path/to/frpc --nginx /path/to/nginx
```

真实协议测试覆盖多 Host 路由、token 拒绝、服务端身份拒绝、明文连接拒绝，以及 Nginx 路径/头转发、SSE 首事件实时输出和 WebSocket 101。测试只启动临时本地进程，不连接或停止现有隧道。

默认不把本地业务健康作为整个 Nginx 的启动依赖；单个本地服务离线不能导致三个静态站点下线。首次迁移先保持旧通道，回滚域名解析并停止新托管客户端即可；不删除本地业务数据。数据、传输证书、token 和服务器 `.env` 应独立备份。

## 6. 2026-10-08 第一阶段验收

- [Actions 37742310291](https://github.com/ReDeployment/XDocker/actions/runs/37742310291) 已发布并验证独立镜像，服务器运行固定 SHA 标签 `ghcr.io/redeployment/xdocker-frps:sha-e0bdee9c7cd89f0eba0d941ae5c87b32f4cf229f`。包保持 Private，未改变可见性。
- 服务器 FRPS 健康，仅公开 7000，8080 只在 Docker 网络内监听；token、传输私钥和真实配置位于服务器 `data/frp/`，权限 600、目录 700。本地只接收客户端配置和服务端公开证书，不接收私钥或 GHCR PAT。
- 私有镜像直连较慢时，使用服务器已有 Docker 凭据配合临时 SSH 转发到本机网络线路，daemonless 导出后导入 Docker。TLS 校验保持开启，未修改 Docker daemon 配置；临时转发已关闭。
- 本机新增独立 LaunchAgent 托管 FRPC，原 FRPC 继续连接旧服务器。新客户端登录及 5 个代理注册成功，执行会话结束后仍存活。
- 外部指定新 IP 验证五个域名的 `/healthz`，经 Nginx → FRPS → 本地 FRPC → 本地业务，全部返回 200。三个静态站点的公网 HTTPS 继续返回 200。
- 47 项自动化测试通过；临时真实 FRP/Nginx 测试验证了 token、服务端身份、明文连接拒绝、多 Host、路径/头、SSE 与 WebSocket。
- 本阶段未切换这五个业务域名的 DNS，也未为其签发新网站证书。下一阶段完成 debug 三域名组、Shopify 和 CourtMate 的 DNS/HTTPS 迁移；企业微信验证文件也需核对迁移。传输证书到期轮换和 Mac 重启后实际自动恢复仍待验收。

## 7. 2026-10-08 第二阶段：业务域名 HTTPS

用户随后已将五个 FRP 域名解析到 `160.202.238.184`，均未查到 AAAA。DNS 只负责把请求送到服务器，XDocker 仍需要站点入口、FRP 映射、证书及运行中的本地业务。本次在已有 HTTP 路由和代理基础上完成以下配置：

| 域名 | 本地端口 | 网站证书组 |
| --- | --- | --- |
| `debug-ecommerce.artisan-cloud.com` | 8091 | `debug-ecommerce.artisan-cloud.com` |
| `debug-scrm.artisan-cloud.com` | 8092 | `debug-ecommerce.artisan-cloud.com` |
| `debug.artisan-cloud.com` | 8078 | `debug-ecommerce.artisan-cloud.com` |
| `shopify.artisan-cloud.com` | 8110 | `shopify.artisan-cloud.com` |
| `court-mate-api-dev.artisan-cloud.com` | 8111 | `court-mate-api-dev.artisan-cloud.com` |

服务器私有清单启用了 debug 组和 Shopify，并增加 CourtMate 的独立证书项，保留三个静态站证书。debug 三域名的完整 SAN 未缩减。新证书到期时间分别为：

- debug 三域名组：`2027-01-06T07:06:53+00:00`。
- Shopify：`2027-01-06T07:07:41+00:00`。
- CourtMate：`2027-01-06T07:14:38+00:00`。

测试签发、正式签发、公网证书信任和本地证书匹配验证完成；HTTP 301 跳转 HTTPS，五个 HTTPS `/healthz` 均返回 200。三组新证书续期 dry-run 最终全部通过；CourtMate 首次测试签发和 debug 首次续期测试出现二次验证节点连接超时，复测成功，供应商多地区 80 端口链路稳定性仍需观察。

旧 Nginx 内联提供的企业微信验证响应已迁移为 `data/acme/verification/WW_verify_L7QyPRfjldgxXN5t.txt`，通过三个 debug 域名的 HTTPS 返回内容与旧入口 SHA256 一致。共享续期服务重新加载后管理 6 组证书、8 个域名。

用户明确 `powerx`、`powerx-dev`、`openclaw`、`ai` 是后续远程业务服务，不在本批穿透迁移范围。它们的 DNS 指向新 IP 不代表后端已接入；本轮未为其创建代理或启动业务容器。三个静态站点 HTTPS 继续正常，旧 FRPC 通道保留用于回滚。


## 8. 本地 PowerX Core 与插件入口

| 域名 | 本地目标 | 健康检查 |
| --- | --- | --- |
| `debug-powerx-local.artisan-cloud.com` | `127.0.0.1:8077`，PowerX Core API | `/api/v1/health`；入口 `/healthz` 映射到同一接口 |
| `debug-powerxplugin-local.artisan-cloud.com` | `127.0.0.1:8078`，PowerX Base Plugin | `/healthz` |

`powerx-dev.artisan-cloud.com` 是服务器 Docker 实例，与这两个本地穿透入口独立。Core API 的根路径可能返回 404，不能据此判断穿透失败；本地进程停止或电脑离线时，新域名后端也会不可用。

新增域名的 A 记录都指向 `160.202.238.184`。服务器同步本仓库后，在根 `.env` 中启用 `debug-powerx-local` 和 `debug-powerxplugin-local`，移除旧 `debug`。用 `hosting.sh init` 创建新站点私有 `.env`，原服务配置保留。初始化需要主机 Python 3；以 root 运行时，新建的站点/服务 `.env` 归属根 `.env` 的所有者，权限 600，使同一部署用户运行的控制台可读取，不修改已有配置的权限。客户端已有 `data/frp/frpc.ini` 不会随模板自动更新：备份后把 8078 的子域名改为 `debug-powerxplugin-local`，并添加模板中的 `powerx_core_api` 段指向 8077；验证配置后重启本仓库托管的客户端，不停止旧服务器的独立 FRPC。

完成 HTTP 端到端验证后，启用 Core 的独立证书项；旧 debug 三域名组在私有清单中换为新 lineage `debug-powerxplugin-local.artisan-cloud.com`，SAN 为 `debug-ecommerce.artisan-cloud.com`、`debug-scrm.artisan-cloud.com`、`debug-powerxplugin-local.artisan-cloud.com`。先测试签发、正式签发，再把 ecommerce、scrm 的私有 `SITE_CERT_NAME` 改为新 lineage，重载三条 HTTPS 路由。旧证书文件保留，新清单不再为旧 debug 域名自动续期。

```bash
sudo sh scripts/hosting.sh http debug-powerx-local --no-pull
sudo sh scripts/hosting.sh http debug-powerxplugin-local --no-pull
sudo sh scripts/hosting.sh issue-test debug-powerx-local
sudo sh scripts/hosting.sh issue debug-powerx-local
sudo sh scripts/hosting.sh issue-test debug-powerxplugin-local
sudo sh scripts/hosting.sh issue debug-powerxplugin-local
sudo sh scripts/hosting.sh https debug-powerx-local
sudo sh scripts/hosting.sh https debug-powerxplugin-local
sudo sh scripts/hosting.sh https debug-ecommerce
sudo sh scripts/hosting.sh https debug-scrm
sudo sh scripts/hosting.sh renew-test debug-powerx-local
sudo sh scripts/hosting.sh renew-test debug-powerxplugin-local
sudo sh scripts/certificates.sh check --report /var/lib/certbot-events/certificates-check.json
curl -fsS https://debug-powerx-local.artisan-cloud.com/api/v1/health
curl -fsS https://debug-powerxplugin-local.artisan-cloud.com/healthz
```

旧 `data/nginx/debug.conf` 在新入口验收后移至私有备份目录，不留在 Nginx 加载目录；旧 DNS 可以删除。仅更新客户端模板不会改变正在运行的代理，必须验证新代理注册和上述公网请求。FRP 站点可设置 `SITE_HEALTH_PATH`（仅允许安全绝对路径），以让统一 `/healthz` 检查真正到达服务健康接口；默认仍为 `/healthz`。

### 2026-10-09 运行验收

两个新域名的 A 记录均为 `160.202.238.184`，未查到 AAAA。托管 FRPC 注册了 8077 Core 和 8078 插件代理，旧 `debug` 代理及 Nginx 路由已撤下。公网 Core 的 `/api/v1/health`、其 `/healthz` 别名及插件 `/healthz` 均为可信 HTTPS 200；控制台显示相应端口，两条站点检查任务成功。

Core 独立证书有效至 `2027-01-07T06:21:56+00:00`；包含 ecommerce、scrm、新插件域名的证书有效至 `2027-01-07T06:23:00+00:00`。测试签发、正式签发和两组模拟续期均已通过，自动续期清单不再依赖旧 debug 域名。插件组第一次续期测试遇到 scrm 的 ACME 二次验证连接超时，复测通过；保留这一实际网络问题记录。

82 项检查和临时真实 FRP/Nginx 测试通过，覆盖健康路径转换、Host/路径/转发头、TLS/token 校验、SSE 与 WebSocket。原有网站、数据库、共享 Nginx 和 FRPS 的容器 ID/启动时间保持不变；Admin 为刷新模板挂载而重建，访问令牌保留。管理服务启动时发现新配置所有权不符，修正后恢复健康；初始化脚本已修复新建私有配置的所有权。旧服务器 FRPC 保留，私有客户端配置、原站点配置、旧证书与退役路由均有回退副本。
