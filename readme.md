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
scripts/runtime-images.sh      # Docker Hub / 自有 GHCR 副本切换
docs/guides/server-setup/      # 新机用户、SSH、Git、Docker 与首次验证
docs/guides/certificates/      # 新机容器证书操作与可选旧机兼容
docs/guides/hosting/           # 多站点部署与扩展指南
docs/guides/powerxdoc/         # PowerXDoc 镜像推送与使用指南
```

## 开始使用

Docker Hub 超时时可按 [自有 GHCR 镜像副本指南](docs/guides/runtime-images/README.md) 同步运行镜像并切换现有 `.env`，无需改变网站源码或证书。

**GHCR 镜像可以保持 Private，不必公开。** 服务器使用 PAT classic + read:packages 认证，账号同时须有目标包 Read 权限；本仓库服务器命令使用 sudo，所以登录使用 `sudo docker login ghcr.io -u YOUR_GITHUB_USERNAME`。详细操作见 [私有镜像认证指南](docs/guides/ghcr-auth/README.md)。

按这个顺序阅读：

1. [服务器准备指南](docs/guides/server-setup/README.md)：Ubuntu 用户、SSH 公钥、root 密钥登录、VS Code、Git、Docker 安装与换源。
2. [证书管理指南](docs/guides/certificates/README.md)：先验证 Certbot 容器，再验证域名挑战路径与签发。
3. [多站点部署指南](docs/guides/hosting/README.md)：部署网站、HTTPS、更新与回滚。
4. [PowerXDoc 镜像推送指南](docs/guides/powerxdoc/README.md)：需要发布新镜像时使用。

已有服务器完成准备后，使用 ubuntu 账号初始化。下面的服务器 Docker 操作使用 sudo：

```bash
sh scripts/hosting.sh init
# 编辑 .env 和 sites/powerxdoc/.env
sudo sh scripts/hosting.sh compose config --quiet
sudo sh scripts/hosting.sh http powerxdoc
```

后续按站点申请证书并启用 HTTPS：

```bash
sudo sh scripts/hosting.sh issue-test powerxdoc
sudo sh scripts/hosting.sh issue powerxdoc
sudo sh scripts/hosting.sh https powerxdoc
```

服务器只需要 Docker、Compose 和 POSIX shell，不需要宿主机安装 Nginx、Certbot 或 Node。

证书清单独立于网站配置，已登记 8 份证书/10 个域名；新服务器执行 `sudo sh scripts/certificates.sh list` 和 `sudo sh scripts/certificates.sh preflight`；旧机的 `--host` 只是可选兼容入口。详细步骤见 [证书管理指南](docs/guides/certificates/README.md)。

完整步骤见 [多站点部署指南](docs/guides/hosting/README.md) 和 [PowerXDoc 镜像推送指南](docs/guides/powerxdoc/README.md)。

所有 Compose 操作通过 `sh scripts/hosting.sh compose ...` 执行，它按 `ENABLED_SITES` 合并服务并加载各站点镜像变量。直接运行根目录 `docker compose up` 只会操作公共服务。

`.env`、各站点的 `.env` 和 `data/` 不提交 Git。证书、私钥及 ACME 账户需要独立备份。XDocker 配置推送到 GitHub，每个网站镜像独立推送到 GHCR；本仓库无需打包为一个大镜像。本阶段不自动部署生产服务器。
