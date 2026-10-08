# ArtisanCloud 官网镜像与部署

官网使用 `/private/var/www/html/ArtisanCloud/ArtisanCloudWebSite/ArtisanCloudHome`，对应 [ArtisanCloud/artisan-cloud-home](https://github.com/ArtisanCloud/artisan-cloud-home) 的默认分支 `develop`。这是 Vue 3 + Vite 项目，构建命令为 `npm run build`，产物为 `dist/`。

小写目录 `artisan-cloud-home` 是历史 `main` 分支的 Nuxt 2 副本，已从本地项目目录移除。旧内容已完整备份，Git 历史保留；新部署使用大写目录，不混用旧 Nuxt 构建和发布配置。

## 1. 本地验证与独立发布

源码仓库维护 Dockerfile、Nginx 配置和 `.github/workflows/docker-publish.yml`，镜像为 `ghcr.io/artisancloud/artisan-cloud-home`。推送 `develop`、`v*` 标签或手动触发后发布 AMD64、ARM64；默认分支生成 `latest`、`develop` 和 `sha-<完整提交 SHA>`。

```bash
cd /private/var/www/html/ArtisanCloud/ArtisanCloudWebSite/ArtisanCloudHome
npm ci --no-audit --no-fund
npm run build
# 按实际修改选择要提交的文件，保留无关改动。
git add Dockerfile .dockerignore docker README.md .github/workflows/docker-publish.yml
git commit -m "update ArtisanCloud website image"
git push origin develop
git rev-parse HEAD
```

本次发布提交为 `30b8b4a17f575f44552dd75c05b3d737694d2137`；[发布工作流](https://github.com/ArtisanCloud/artisan-cloud-home/actions/runs/37735140140) 的构建、推送和实际容器验证均已成功。后续发布同样须检查全部步骤成功后再部署。镜像网页入口为 [GitHub Packages](https://github.com/orgs/ArtisanCloud/packages/container/package/artisan-cloud-home)。若包为 Private，先按 [认证指南](../ghcr-auth/README.md) 登录，无需公开包。

已核对 AMD64 manifest 为 `sha256:86df8b2c7b82db93eadaf767053d9a94e9052bb5b01b6b33b34103199b85e1a1`，ARM64 为 `sha256:a07e6cb53618280f418efeb07705a0039f5252e5e1c496fe0d882a367c3fd217`。服务器导入的是校验后的 AMD64 包，镜像 revision 与发布提交一致。

本地干净构建及临时 Nginx 路由检查通过，生成的主 JS/CSS 文件名与迁移前线上一致。开发机 Docker daemon 未运行，镜像运行由 Actions 和服务器验证；浏览器逐页面交互需单独确认。

## 2. Vue Router 与静态资源

官网使用 `createWebHistory()`。容器 Nginx 对 `/products`、`/products/:slug`、`/cases`、`/contact`、`/contact-by-email` 等路径回退到 `index.html`，以支持刷新和直接打开。Vue 渲染页面仍依赖浏览器执行 JS，HTTP 200 不能替代界面交互验证。

`/assets/`、`/images/`、`/ads.txt` 等静态资源按实际文件提供，缺失文件返回 404，不返回 SPA HTML。未知的前端路由也会拿到入口文档，由现有 Vue Router 处理；这与 VitePress 文档站的服务器 404 规则不同。

## 3. 新服务器先准备 HTTP

先在本地更新 XDocker 模板、验证并推送；服务器同步相同提交后，修改私有配置并启动。根 `.env` 保留公共设置，将启用列表设为：

```dotenv
ENABLED_SITES="powerxdoc powerwechat artisancloud-home"
```

在 `sites/artisancloud-home/.env` 设置：

```dotenv
SITE_DOMAIN=artisan-cloud.com
SITE_IMAGE=ghcr.io/artisancloud/artisan-cloud-home:sha-30b8b4a17f575f44552dd75c05b3d737694d2137
```

```bash
sudo docker pull --platform linux/amd64 \
  ghcr.io/artisancloud/artisan-cloud-home:sha-30b8b4a17f575f44552dd75c05b3d737694d2137
sudo sh scripts/hosting.sh compose config --quiet
sudo sh scripts/hosting.sh http artisancloud-home --no-pull
curl -I -H 'Host: artisan-cloud.com' http://127.0.0.1/
curl -I -H 'Host: artisan-cloud.com' http://127.0.0.1/products
```

下载线路慢时使用 [本机导出、SSH 上传与校验导入](../runtime-images/README.md)，确认完整镜像存在后再启动。切换 DNS 前可从外部使用 `curl --resolve artisan-cloud.com:80:160.202.238.184 http://artisan-cloud.com/` 检查新入口。

HTTP 和静态资源检查通过后，只将 `artisan-cloud.com` 的 A 记录切到新服务器 `160.202.238.184`；存在 AAAA 时同步处理，不改已经完成迁移的子域名。此次证书仅覆盖 `artisan-cloud.com`，不自动新增 `www`。

## 4. 证书、HTTPS 与续期

在服务器私有 `certbot/certificates.local.json` 启用已有的 `artisan-cloud.com` 证书项，保留 PowerXDoc 和 PowerWechat。根 `.env` 保留真实联系邮箱及库存路径。公网 DNS 和挑战路径通过后：

```bash
sudo sh scripts/hosting.sh issue-test artisancloud-home
sudo sh scripts/hosting.sh issue artisancloud-home
sudo sh scripts/hosting.sh https artisancloud-home
sudo sh scripts/certificates.sh verify artisan-cloud.com --match-local
sudo sh scripts/hosting.sh renew-test artisancloud-home
sudo sh scripts/hosting.sh status
```

检查三个域名的 HTTPS、证书、首页和资源，并确认官网直链刷新正常。一个共享 `certbot-renew` 管理本机启用证书，无需重复安装宿主机 Nginx、Certbot 或 Node。主动续期测试跳过随机调度等待，普通自动续期保留原有调度。

## 5. 2026-10-08 部署记录

用户已将 `artisan-cloud.com` 解析到 `160.202.238.184`，未查到 AAAA。服务器上的官网容器健康；HTTP-01 测试签发、正式签发、公网 TLS 信任与本地证书匹配检查成功，正式证书有效期至 `2027-01-06T05:08:13+00:00`。

公网 HTTP 301 跳转 HTTPS，HTTPS 首页及产品、案例、联系页面的 SPA 入口返回 200；JS/CSS MIME 正确，主 JS 与迁移前线上文件内容一致；缺失 JS 和图片返回 404。PowerXDoc、PowerWechat 的公网 HTTPS 继续返回 200。

首次续期 dry-run 出现二次验证节点连接 80 端口超时，复测成功。供应商入站链路的多地稳定性仍需持续观察，不能用一次客户端 200 保证未来验证总能通过。共享续期服务已重新加载包含官网、PowerWechat、PowerXDoc 的清单；其他 5 组证书尚未迁移。

浏览器连接工具不可用，本次未做浏览器逐页面交互验收。真实到期续期后的自动 reload 和服务器重启恢复也尚未实测。
