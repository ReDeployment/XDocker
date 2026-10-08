# PowerWechat 静态网站镜像与部署

源码仓库为 [ArtisanCloud/PowerWechatDocs](https://github.com/ArtisanCloud/PowerWechatDocs)，默认分支 `release/3.0.1`。本机路径为 `/private/var/www/html/ArtisanCloud/PowerWechat/PowerWechatDocs`。源码仓库维护 Dockerfile、静态站 Nginx 配置和发布工作流；XDocker 管理域名入口、证书和站点生命周期。

## 1. 已验证的镜像

- 镜像：`ghcr.io/artisancloud/powerwechat-docs:sha-a23242c98e26e7da1e4cf2c6dadfc38030744efd`。
- 发布提交：`a23242c98e26e7da1e4cf2c6dadfc38030744efd`。
- [Actions 发布与运行检查](https://github.com/ArtisanCloud/PowerWechatDocs/actions/runs/37731802946) 已成功，发布 Linux AMD64、ARM64，并实际运行 AMD64 镜像验证首页、文档直链、图片和未知路径 404。
- [GHCR 镜像包页面](https://github.com/orgs/ArtisanCloud/packages/container/package/powerwechat-docs)。`ghcr.io` 是 registry 地址，网页管理入口位于 GitHub Packages。

本地使用 Node 22 执行 `npm ci`、`npm run docs:build` 成功，生成 103 个页面，并用本机临时 Nginx 验证路由。开发机 Docker daemon 未运行；容器验收由 Actions 和目标服务器完成。

## 2. 后续修改与推送

先在本地修改、构建和验证，再提交推送：

```bash
cd /private/var/www/html/ArtisanCloud/PowerWechat/PowerWechatDocs
npm ci --no-audit --no-fund
npm run docs:build
git status --short
# 只添加本次需要发布的修改。
git add Dockerfile .dockerignore docker .github/workflows/docker-publish.yml
git commit -m "update PowerWechat documentation image"
git push origin release/3.0.1
git rev-parse HEAD
```

默认分支推送会同时触发现有 GitHub Pages 和独立 Docker 发布流程。镜像标签包括 `latest`、`release-3.0.1` 和 `sha-<完整提交 SHA>`，服务器使用经过检查的 SHA 标签。先确认 Docker 发布工作流成功，再更新 XDocker 本地的镜像模板及文档、验证并推送；服务器同步相同 XDocker 提交后才能运行新配置，不在远程修改仓库代码。

## 3. 新服务器先准备 HTTP，再切换域名

服务器更新 XDocker 后运行 `sh scripts/hosting.sh init`；它保留已经存在的 `.env`。根 `.env` 保留现有公共设置，将启用列表设为：

```dotenv
ENABLED_SITES="powerxdoc powerwechat"
```

在 `sites/powerwechat/.env` 设置：

```dotenv
SITE_DOMAIN=powerwechat.artisan-cloud.com
SITE_IMAGE=ghcr.io/artisancloud/powerwechat-docs:sha-a23242c98e26e7da1e4cf2c6dadfc38030744efd
```

若镜像包为 Private，先按 [GHCR 认证指南](../ghcr-auth/README.md) 登录，账号须有该包的 Read 权限。使用 sudo 的 Docker 凭据与 ubuntu 普通用户的凭据独立。

```bash
sudo docker pull --platform linux/amd64 \
  ghcr.io/artisancloud/powerwechat-docs:sha-a23242c98e26e7da1e4cf2c6dadfc38030744efd
sudo sh scripts/hosting.sh compose config --quiet
sudo sh scripts/hosting.sh http powerwechat --no-pull
sudo sh scripts/hosting.sh status
curl -I -H 'Host: powerwechat.artisan-cloud.com' http://127.0.0.1/
curl -I -H 'Host: powerwechat.artisan-cloud.com' http://127.0.0.1/zh/start/installation
```

服务器下载线路慢时，按 [本地导出、SSH 上传与校验导入](../runtime-images/README.md#42-下载线路不稳定时本机导出ssh-上传服务器导入) 操作，导出实际 SHA 标签的目标平台镜像，检查完整镜像存在后再使用 `--no-pull`。不要把 PowerWechat 请求发送给 PowerXDoc 的站点服务；两者通过 Host 和独立服务分流。

HTTP 入口和文档直链检查通过后，将 **仅 `powerwechat.artisan-cloud.com`** 的 A 记录切到新服务器公网 IP `160.202.238.184`；存在 AAAA 时需同步处理。新服务器的供应商须允许该域名正常公网访问。DNS 切换后，先从其他网络检查首页和 `/.well-known/acme-challenge/` 测试文件，不要仅依据本机 200 判断签发条件已经满足。

## 4. 独立证书与 HTTPS

服务器使用 `certbot/certificates.local.json` 时，只将 `powerwechat.artisan-cloud.com` 对应项的 `enabled` 改为 true，保留已经启用的 PowerXDoc 和其他服务器实际负责的项。确认根 `.env` 的 `CERTIFICATES_INVENTORY` 指向该文件、`CERTBOT_EMAIL` 是实际联系邮箱；这些私有配置不提交 Git。

公网挑战路径确认成功后：

```bash
sudo sh scripts/hosting.sh issue-test powerwechat
sudo sh scripts/hosting.sh issue powerwechat
sudo sh scripts/hosting.sh https powerwechat
sudo sh scripts/certificates.sh verify powerwechat.artisan-cloud.com --match-local
sudo sh scripts/hosting.sh renew-test powerwechat
```

测试签发失败时不要继续正式签发。HTTP-01 的多地验证和公网异常响应排查见 [证书指南](../certificates/README.md#24-公网入口恢复后仍需验证签发与续期)。HTTPS 成功后检查两个站点的首页、文档、资源与证书，确认 PowerXDoc 继续正常服务。

后续更新只执行 `sh scripts/hosting.sh update powerwechat`，共用一个 `certbot-renew` 服务管理本机已启用证书，不为每个网站重复安装 Nginx、Certbot 或宿主机 Node。
