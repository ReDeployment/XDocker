# FRP 穿透服务与多域名入口

服务器运行一个独立 `frps` 容器，本地运行 `frpc`，多个业务域名共用 FRPS。固定版本为 0.52.3，与现有本地 ARM64 客户端及旧服务器一致；版本升级单独验证。

```text
浏览器 HTTPS → Nginx → frps:8080 → TLS 隧道 → 本地 frpc → 本地业务服务
本地 frpc 主动连接服务器 7000；8080 仅供 Docker 网络内 Nginx 使用。
```

## 1. 文件与私有配置

- `services/frps/`：镜像构建、Compose 和环境模板；镜像无生产 token 或私钥。
- `clients/frpc/main.ini.example`：5 个业务映射模板，默认对应本地 8091、8092、8078、8110、8111。
- `sites/debug-ecommerce`、`debug-scrm`、`debug`、`shopify`、`court-mate-api-dev`：域名与证书组，类型为 `frp_http`，无需独立站点镜像。
- `data/frp/`：真实 token、传输证书、私钥、客户端配置和日志，整个目录被 Git 忽略。
- `scripts/frp.sh`：初始化、检查、启动和本地客户端生命周期；不自动修改旧客户端配置。

FRPS 启用 token 和强制 TLS。客户端使用通过可信 SSH 下载的 `server.crt` 验证服务端身份，不只启用加密。传输证书是专用自签证书，与浏览器 HTTPS 的 Let's Encrypt 证书独立；不会让浏览器信任自签证书。初始化证书有效期为 365 天，需在到期前协调更新服务端证书及客户端信任文件，保留私有配置备份。

## 2. 发布与服务器启动

所有代码先本地修改、验证并推送，再让服务器同步相同提交。`.github/workflows/publish-frps.yml` 发布 `ghcr.io/redeployment/xdocker-frps:0.52.3` 和完整 SHA 标签，包含 AMD64、ARM64。构建下载官方发行文件并核对 [官方校验和](https://github.com/fatedier/frp/releases/download/v0.52.3/frp_sha256_checksums.txt)。查看 [镜像包页面](https://github.com/orgs/ReDeployment/packages/container/package/xdocker-frps) 和 Actions 运行检查；Private 包按 [GHCR 认证指南](../ghcr-auth/README.md) 登录。

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

`start-client` 用于并行验证，会脱离当前终端继续运行，但尚未提供 macOS 重启自启动配置。FRPC 0.52.3 登录失败可能仍返回退出码 0，必须核对进程、登录/注册日志及真实业务请求。根路径 404 对 API 服务可能正常，应使用实际健康接口、API 或 WebSocket 做验收。

## 4. 域名与证书迁移

根 `.env` 把所需 FRP 站点加入 `ENABLED_SITES`，保留三个静态站点。`services/frps/.env` 的 `FRPS_SUBDOMAIN_HOST` 必须与客户端子域名后缀一致。

```bash
sudo sh scripts/hosting.sh http debug-ecommerce --no-pull
sudo sh scripts/hosting.sh http debug-scrm --no-pull
sudo sh scripts/hosting.sh http debug --no-pull
```

外部使用 `curl --resolve debug-ecommerce.artisan-cloud.com:80:160.202.238.184 http://debug-ecommerce.artisan-cloud.com/实际健康路径` 检查新入口，再切换域名。Nginx 保留 Host、转发协议头、WebSocket Upgrade；关闭响应缓冲以支持 SSE。

原 debug 证书组包含三个域名，必须一起满足 DNS/HTTP-01 验证，不能缩减 SAN。服务器私有证书清单启用该组后，执行 `issue-test debug-ecommerce`、`issue debug-ecommerce`，再逐个 `https` 激活三个站点。Shopify 使用自己的证书项。

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
