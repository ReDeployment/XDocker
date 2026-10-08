# XDocker 多站点全容器部署指南

从新机器开始时，先完成 [服务器准备指南](../server-setup/README.md)，其中包含 Ubuntu 用户、SSH、VS Code、Git 与 Docker 安装。本文面向已经能运行 Docker 的服务器。使用 Private GHCR 镜像时先完成 [PAT 与 sudo Docker 登录](../ghcr-auth/README.md)，无需公开镜像。Ubuntu 新机默认使用 sudo；本地 Mac Docker Desktop 验证则按第 3 节直接使用本机 Docker。

## 1. 配置边界

根目录 `compose.yml` 只维护公共入口 Nginx、一次性 Certbot 工具和持续续期服务。每个 `sites/<key>/` 有独立服务与镜像配置。网站容器不对宿主机暴露端口，通过共享 Compose 网络访问。

根目录 `.env` 管理共享参数：

```dotenv
ENABLED_SITES="powerxdoc"
CERTBOT_EMAIL=your-real-email@example.com
HTTP_BIND=0.0.0.0
HTTP_PORT=80
HTTPS_BIND=0.0.0.0
HTTPS_PORT=443
```

每个 `sites/<key>/.env` 只管理本站：

```dotenv
SITE_DOMAIN=powerx-doc.artisan-cloud.com
SITE_IMAGE=ghcr.io/artisancloud/powerxdoc:sha-94cb21895e8bf42c45a9bc55fa6f711b7d3d0572
```

这些 `.env` 文件是可信的 shell 配置，使用 `KEY=value` 格式。初始化不覆盖已有文件。镜像版本和域名不放在公共 `.env` 中。共享 Nginx、Certbot 的镜像可通过根目录 `NGINX_IMAGE`、`CERTBOT_IMAGE` 固定为已验证的版本或 digest。

默认只启用 PowerXDoc。PowerWechatDocs 已有经过 Actions 运行验证的 SHA 镜像，接入步骤见 [PowerWechat 静态站指南](../powerwechat/README.md)。ArtisanCloudHome 的镜像版本仍是占位值；必须先发布实际镜像、填写对应 `SITE_IMAGE`，再加入 `ENABLED_SITES`。脚本会拒绝启用尚未替换的占位镜像。

## 2. 推送配置与服务器下载

每个网站在自己的源码仓库构建并推送 GHCR。PowerXDoc 的具体推送命令见 [镜像推送指南](../powerxdoc/README.md)。XDocker 配置提交到 GitHub：

```bash
cd /private/var/www/html/ArtisanCloud/X/XDocker
git status --short
git add .gitignore .env.example compose.yml readme.md sites nginx certbot scripts tests docs/guides
git commit -m "feat: support independent sites with shared nginx and certbot"
git push origin HEAD
```

服务器需要 Docker Engine、Docker Compose 和 POSIX shell。SSH Deploy key 与指定密钥的下载命令见 [准备指南第 6 节](../server-setup/README.md)。首次下载：

```bash
mkdir -p ~/workspace
cd ~/workspace
git clone git@github.com:ReDeployment/XDocker.git
cd XDocker
sh scripts/hosting.sh init
```

服务器需有仓库读取权限；clone 的分支必须包含本次配置。若你推送到非默认分支，用 `git clone --branch <实际分支> ...` 或已有 checkout 切换到该分支。实际 `.env`、证书和 `data/` 已被忽略，不会上传 GitHub。

## 3. 本地 HTTP 验证

初始化后，根目录 `.env` 设置 `HTTP_BIND=127.0.0.1`、`HTTP_PORT=8080`、`HTTPS_BIND=127.0.0.1`、`HTTPS_PORT=8443`。编辑所选站点 `.env` 的镜像版本，然后：

```bash
sh scripts/hosting.sh compose config --quiet
sh scripts/hosting.sh http powerxdoc
curl -I -H 'Host: powerx-doc.artisan-cloud.com' http://127.0.0.1:8080/
```

入口按域名分流，直接用 localhost 请求未知 Host 会返回 404。浏览器可为调试域名配置本地解析，例如将 `powerx-doc.artisan-cloud.com` 指向 `127.0.0.1`，访问 `http://powerx-doc.artisan-cloud.com:8080`；测试结束后恢复解析，避免影响真实域名访问。localhost 测试不申请真实证书。

也可以先在网站源码目录 `docker build -t powerxdoc:local .`，将本站 `SITE_IMAGE=powerxdoc:local`，使用：

```bash
sh scripts/hosting.sh http powerxdoc --no-pull
```

`--no-pull` 跳过远端镜像拉取。站点健康检查要求运行镜像包含 `wget` 并在 80 端口返回首页；当前模板针对 Nginx Alpine 静态站，接入其他类型服务时按实际运行镜像调整 healthcheck、端口及模板。

## 4. 按站点部署与申请证书

以下用 `powerxdoc` 演示，其他站点替换 key、域名即可。每个站点证书都需登记在 `certbot/certificates.json`（或本机 `.local.json`），其完整 SAN 与 Certificate Name 必须一致。`SITE_CERT_NAME` 可指定共享 SAN 组名称，默认等于 `SITE_DOMAIN`。每个域名 A/AAAA 要正确解析到服务器；公网 TCP 80/443 放行，现有服务不得占用端口。Certbot 使用 HTTP-01 webroot，域名的公网 80 端口必须可达。首次启用一个站点不会改动其他站点的 Nginx 配置或证书。

### 4.1 HTTP

```bash
sudo sh scripts/hosting.sh http powerxdoc
sudo sh scripts/hosting.sh status
curl -I -H 'Host: powerx-doc.artisan-cloud.com' http://127.0.0.1/
mkdir -p data/acme/.well-known/acme-challenge
printf 'xdocker-acme-ok\n' > data/acme/.well-known/acme-challenge/probe
curl http://powerx-doc.artisan-cloud.com/.well-known/acme-challenge/probe
```

脚本拉取选定的网站镜像与公共工具，等待该网站健康，再启用它自己的 HTTP 配置。挑战地址应返回 `xdocker-acme-ok`，还应从其他网络确认公网可达。成功后删除 probe。

### 4.2 测试与正式签发

```bash
sudo sh scripts/hosting.sh issue-test powerxdoc
sudo sh scripts/hosting.sh issue powerxdoc
```

第一步是 dry-run，不保存测试证书。第二步正式签发，会同意 Let's Encrypt 服务条款并使用配置邮箱注册账户。证书名称为本站域名，写入共享的 `data/letsencrypt/live/<域名>/`，其他域名使用独立证书。

失败时检查 DNS、错误 AAAA、80 端口和挑战路径，不要反复尝试正式签发。

### 4.3 HTTPS

```bash
sudo sh scripts/hosting.sh https powerxdoc
curl -I http://powerx-doc.artisan-cloud.com/
curl -I https://powerx-doc.artisan-cloud.com/
sudo sh scripts/hosting.sh renew-test powerxdoc
```

预期 HTTP 301，HTTPS 200 且证书验证成功，本站续期 dry-run 成功。HTTPS 操作只替换 `data/nginx/powerxdoc.conf`。证书缺失时不切换；Nginx 检查或 reload 失败时恢复该站前一文件。不要同时运行多个配置切换命令。

## 5. 接入其他站点

目录已提供 `powerwechat` 和 `artisancloud-home` 的静态站模板。PowerWechatDocs 已发布验证镜像；ArtisanCloudHome 仍需先构建并发布镜像。

1. 在对应源码仓库发布独立镜像，并用 `docker buildx imagetools inspect <镜像>` 确认标签存在。
2. 编辑 `sites/powerwechat/.env` 或 `sites/artisancloud-home/.env`，填写真实域名与镜像版本。
3. 在公共 `.env` 扩展启用列表，例如：

```dotenv
ENABLED_SITES="powerxdoc powerwechat artisancloud-home"
```

4. 验证合并结果，只对新站执行启动与签发：

```bash
sudo sh scripts/hosting.sh compose config --quiet
sudo sh scripts/hosting.sh http powerwechat
sudo sh scripts/hosting.sh issue-test powerwechat
sudo sh scripts/hosting.sh issue powerwechat
sudo sh scripts/hosting.sh https powerwechat
```

ArtisanCloudHome 同样替换 key。每个站点独立验证页面、路由、资源、HTTPS；已有 PowerXDoc 配置和镜像不会因新增站点被替换。

接入第四个站点时新增 `sites/<key>/site.conf`、`compose.yml`、`.env.example`。例如：

```sh
# site.conf：每个站点服务名和镜像变量名必须唯一。
SITE_SERVICE=my-docs
SITE_IMAGE_VAR=MY_DOCS_IMAGE
```

```yaml
# compose.yml：镜像变量名与 site.conf 一致。
services:
  my-docs:
    image: ${MY_DOCS_IMAGE:?Initialize site configuration}
    restart: unless-stopped
    healthcheck:
      test: [CMD, wget, -q, --spider, http://127.0.0.1/]
      interval: 30s
      timeout: 5s
      retries: 3
```

```dotenv
# .env.example
SITE_DOMAIN=docs.example.com
SITE_IMAGE=ghcr.io/your-org/my-docs:verified-version
```

运行 `init` 为新增站点复制 `.env`，然后修改实际配置并加入启用列表。通用脚本、公共 Compose 和 Nginx 模板无需追加站点硬编码。脚本拒绝重复域名、服务名及镜像变量名。多个 Compose 文件的路径相对根目录解析，参见 [Docker 合并规则](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/)。

## 6. 日常启动、更新和回滚

所有 Compose 操作使用包装入口，它加载各站点独立配置，再组合公共与已启用的服务：

```bash
sudo sh scripts/hosting.sh compose --profile tls up -d
sudo sh scripts/hosting.sh status
sudo sh scripts/hosting.sh compose logs --tail=100 nginx certbot-renew
```

默认 `.env` 只启用 PowerXDoc，因此这些命令不会拉取或启动另外两个模板。新增已启用站点在 HTTP 配置建立前也不会有公网域名入口。公共 Nginx 不依赖所有网站健康，单个网站故障不会阻止其他网站入口启动。

修改目标站点 `.env` 的 `SITE_IMAGE`，独立升级：

```bash
sudo sh scripts/hosting.sh update powerxdoc
curl -I https://powerx-doc.artisan-cloud.com/
```

只拉取和重建该站点服务。回滚恢复它的上一份镜像标签或 digest，再运行同一命令。共享 Nginx 使用 Docker DNS 动态解析后端，无需修改容器 IP。

停用站点前，先保留启用列表，把它的入口配置移出 `data/nginx/`，检查并 reload，然后停止对应服务：

```bash
mv data/nginx/powerwechat.conf data/powerwechat.conf.disabled
sudo sh scripts/hosting.sh compose exec -T nginx nginx -t
sudo sh scripts/hosting.sh compose exec -T nginx nginx -s reload
sudo sh scripts/hosting.sh compose stop powerwechat-docs
```

确认成功后从 `ENABLED_SITES` 移除 `powerwechat`；证书不会被删除，共享 renew 仍检查已保存证书。若长期停用且 HTTP 验证不再可达，需先备份，再通过 Certbot 正常删除其证书配置，以免持续续期失败。移除启用列表不会自动删除容器、证书或已有域名配置。

## 7. 自动续期与持久化

一个 `certbot-renew` 服务每 12 小时检查独立清单中已启用的证书，保留每份证书的原续期选项。实际叶证书更新后写入共享事件，公共 Nginx 在 30 秒内检测并通过语法检查后 reload。Certbot 没有 Docker socket 权限，无需宿主机 cron。[Certbot 官方指南](https://eff-certbot.readthedocs.io/en/stable/using.html)

独立的检查、旧机续期、完整 SAN 签发及公网验证见 [证书管理指南](../certificates/README.md)。迁移部分证书时使用本机清单禁用仍由旧机负责的项，不要通过网站启用列表隐式忽略证书。

备份根目录及各站点 `.env`、整个 `data/letsencrypt/`、`data/nginx/`，保留证书符号链接和权限，不要只复制 `live/`。升级时不得删除 `data/`。

容器使用 `restart: unless-stopped`。服务器重启后应自动恢复，仍需实际验收；如果执行过 down，使用 `sudo sh scripts/hosting.sh compose --profile tls up -d` 恢复续期服务。

## 8. 从上一版单站配置迁移

仅在实际用过 `scripts/powerxdoc.sh` 并已有 `.env` 或 `data/nginx/default.conf` 时执行；全新部署直接按前文初始化。

1. 备份原 `.env` 与 `data/`，保留旧镜像、域名和邮箱值。
2. 运行新 `init`，它不会覆盖旧 `.env`。
3. 在公共 `.env` 中加入 `ENABLED_SITES="powerxdoc"`，保留公共邮箱和端口。
4. 将旧 `DOMAIN`、`POWERXDOC_IMAGE` 分别复制为 `sites/powerxdoc/.env` 的 `SITE_DOMAIN`、`SITE_IMAGE`，之后删除公共文件里的旧站点变量。
5. 若存在旧 `data/nginx/default.conf`，在初始化生成新默认入口后，将旧文件改名为 `data/nginx/powerxdoc.conf`，不要保留两份同域名配置。
6. 检查合并配置与 Nginx，再执行日常 TLS 启动。项目名称和 PowerXDoc 服务名保持一致，原证书目录无需迁移。

操作方式改为 `sh scripts/hosting.sh <动作> powerxdoc`。旧 `scripts/powerxdoc.sh` 已移除。

## 9. 检查与验收

```bash
sudo sh scripts/hosting.sh list
sudo sh scripts/hosting.sh compose config --quiet
sudo sh scripts/hosting.sh compose --profile tls config --quiet
sudo sh scripts/hosting.sh compose logs --tail=100 nginx powerx-doc certbot-renew
sudo sh scripts/hosting.sh compose exec -T nginx nginx -t
```

开发机可运行 `python3 -m unittest discover -s tests -v`；Python 不是服务器运行依赖。测试使用 Docker stub 检查多站点隔离、证书切换回滚，并使用真实 Compose CLI 检查默认与三站合并结果，不启动容器或签发证书。

开发阶段验证：多站点测试通过，包含真实 Compose CLI 的默认单站和三站合并解析；宿主机临时 Nginx 配合临时测试后端通过三个域名的路由与 ACME 路径检查、两个域名的 HTTPS/SNI 检查，未知域名返回 404。这些测试使用临时自签名证书，没有启动实际 PowerWechatDocs、ArtisanCloudHome 镜像。

2026-10-08 新服务器运行验收：PowerXDoc 容器健康，Nginx 和自动续期容器已启动；HTTP-01 测试签发、正式签发、HTTPS 公网信任及本地证书匹配、续期 dry-run 均通过。`https://powerx-doc.artisan-cloud.com/` 首页、产品概览和静态资源返回 200，HTTP 跳转 HTTPS，未知路径返回 404。证书到期时间为 `2027-01-06T03:19:39+00:00`。本机证书清单只启用 PowerXDoc，其他站点尚未部署。真实到期续期后的自动 reload、服务器重启恢复及独立回滚仍待验收，见准备指南的状态记录。
