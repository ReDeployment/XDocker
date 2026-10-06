# XDocker

XDocker 是多网站容器部署仓库：共享入口 Nginx 和 Certbot，各网站独立配置服务、域名和 GHCR 镜像。

## 架构

```text
公网 80/443 → 共享 Nginx 容器
                ├── PowerXDoc 容器
                ├── PowerWechatDocs 容器（待镜像发布后启用）
                └── ArtisanCloudHome 容器（待镜像发布后启用）
                     ↕
            Certbot 签发与自动续期
```

| 站点 key | Compose 服务 | 域名 | 初始状态 |
| --- | --- | --- | --- |
| `powerxdoc` | `powerx-doc` | `powerx-doc.artisan-cloud.com` | 默认启用，已有验证过的镜像 |
| `powerwechat` | `powerwechat-docs` | `powerwechat.artisan-cloud.com` | 模板已提供，等待实际镜像 |
| `artisancloud-home` | `artisan-cloud-home` | `artisan-cloud.com` | 模板已提供，等待实际镜像 |

## 目录

```text
compose.yml                   # 只定义公共 Nginx、Certbot、续期服务
.env.example                  # ENABLED_SITES、邮箱、公网端口
sites/<site>/
  compose.yml                 # 站点自己的服务
  site.conf                   # 服务名、独立镜像变量名
  .env.example                # SITE_DOMAIN、SITE_IMAGE
scripts/hosting.sh            # 通用的站点操作与 Compose 包装入口
nginx/                        # 公共 HTTP/HTTPS 模板与续期 reload
certbot/                      # 独立 certificates.json 清单与续期
scripts/certificates.sh        # 证书检查、续期、签发、公网验证
docs/guides/certificates/      # 旧机兼容与容器证书操作指南
docs/guides/hosting/           # 多站点部署与扩展指南
docs/guides/powerxdoc/         # PowerXDoc 镜像推送与使用指南
```

## 开始使用

```bash
sh scripts/hosting.sh init
# 编辑 .env 和 sites/powerxdoc/.env
sh scripts/hosting.sh compose config --quiet
sh scripts/hosting.sh http powerxdoc
```

后续按站点申请证书并启用 HTTPS：

```bash
sh scripts/hosting.sh issue-test powerxdoc
sh scripts/hosting.sh issue powerxdoc
sh scripts/hosting.sh https powerxdoc
```

服务器只需要 Docker、Compose 和 POSIX shell，不需要宿主机安装 Nginx、Certbot 或 Node。

证书清单独立于网站配置，已登记 8 份证书/10 个域名；旧服务器可执行 `sudo sh scripts/certificates.sh --host check`，容器入口执行 `sh scripts/certificates.sh check`。详细步骤见 [证书管理指南](docs/guides/certificates/README.md)。

完整步骤见 [多站点部署指南](docs/guides/hosting/README.md) 和 [PowerXDoc 镜像推送指南](docs/guides/powerxdoc/README.md)。

所有 Compose 操作通过 `sh scripts/hosting.sh compose ...` 执行，它按 `ENABLED_SITES` 合并服务并加载各站点镜像变量。直接运行根目录 `docker compose up` 只会操作公共服务。

`.env`、各站点的 `.env` 和 `data/` 不提交 Git。证书、私钥及 ACME 账户需要独立备份。XDocker 配置推送到 GitHub，每个网站镜像独立推送到 GHCR；本仓库无需打包为一个大镜像。本阶段不自动部署生产服务器。
