# PowerXDoc：镜像推送与全容器部署指南

## 1. 两个仓库分别推送什么

| 仓库 | 本地位置 | 推送目标 | 职责 |
| --- | --- | --- | --- |
| PowerXDoc | `/private/var/www/html/ArtisanCloud/X/PowerX/Core/PowerXDocs` | GitHub 源码；Actions 或 Buildx 发布到 GHCR | 独立网站镜像 |
| XDocker | `/private/var/www/html/ArtisanCloud/X/XDocker` | `git@github.com:ReDeployment/XDocker.git` | Compose、Nginx、Certbot、脚本和指南 |

网站镜像为 `ghcr.io/artisancloud/powerxdoc`。服务器 clone XDocker 后由 Compose 拉取这个镜像，以及官方 Nginx、Certbot 镜像。**XDocker 不需要打包为另一个镜像推送到 GHCR。**

当前只编排 PowerXDoc，其他网站以后分别接入。网站镜像中的 Nginx 负责静态文件；入口 Nginx 负责域名、HTTPS 和反向代理。

## 2. 镜像已有一次成功发布

以下为此前实际验证结果，不代表服务器已上线：

- 提交：`94cb21895e8bf42c45a9bc55fa6f711b7d3d0572`，分支 `dev/scenario`。
- [Actions 运行 37180164052](https://github.com/ArtisanCloud/PowerXDoc/actions/runs/37180164052)：`success`。
- 标签：`dev-scenario`、`sha-94cb21895e8bf42c45a9bc55fa6f711b7d3d0572`。
- Digest：`sha256:10964eac4c5128e1808686a3e78c59d464c468ca3630d244bf449a711972fbed`。
- GHCR manifest 已确认 `linux/amd64`、`linux/arm64`，当时可匿名读取。
- 该镜像作为 `.env.example` 的初始版本，可直接拉取验证，无需先重新构建。

## 3. 推送方式 A：GitHub Actions（推荐）

镜像文件在 PowerXDoc 仓库，不在 XDocker：`Dockerfile`、`.dockerignore`、`docker/nginx.conf`、`.github/workflows/docker-publish.yml`。Node 22 构建 `docs/website`，输出 `docs/.vitepress/dist`，运行镜像仅包含 Nginx 和静态产物。

### 3.1 推送开发镜像

```bash
cd /private/var/www/html/ArtisanCloud/X/PowerX/Core/PowerXDocs
git branch --show-current
git status --short
```

确认使用 `dev/scenario`。提交本次实际修改的文件，再推送：

```bash
git add Dockerfile .dockerignore docker/nginx.conf .github/workflows/docker-publish.yml
git commit -m "ci: update PowerXDoc container publishing"
git push origin dev/scenario
```

上面四个配置文件此前已经提交和推送；没有新修改时跳过 `git add` 和 `git commit`。单纯重复 push 不会触发新构建。已有镜像可直接使用；重建可按下面的版本 tag 方法触发。以后网站内容更新时提交实际改动，再 push 同样会自动构建。

| 触发 | 镜像标签 |
| --- | --- |
| push `dev/scenario` | `dev-scenario`、`sha-<完整提交 SHA>` |
| push `dev/michaelhu` | `latest`、`dev-michaelhu`、SHA 标签 |
| push `v*` tag | 原始版本标签，例如 `v1.0.2`、SHA 标签 |

版本 tag 不更新 `latest`，开发分支也不覆盖 `latest`。

### 3.2 发布明确的版本标签

先确认当前提交就是要发布的版本，选择尚未使用的版本号。以下 `v1.0.2` 仅为示例；先检查远端，若已存在则换新版本号：

```bash
git log -1 --oneline
git ls-remote --tags origin refs/tags/v1.0.2
git tag v1.0.2
git push origin refs/tags/v1.0.2
```

预期生成 `ghcr.io/artisancloud/powerxdoc:v1.0.2`。不要移动已发布的版本 tag。

### 3.3 在 GitHub 确认发布

1. 打开 [PowerXDoc Actions](https://github.com/ArtisanCloud/PowerXDoc/actions/workflows/docker-publish.yml)。
2. 点击与本次分支、提交或版本对应的运行记录。
3. 等待 `Build and push Docker image` 和整个运行变为绿色 `Success`。
4. 打开 [PowerXDoc Package](https://github.com/orgs/ArtisanCloud/packages/container/package/powerxdoc)，确认目标标签及 digest。
5. 确认包含 AMD64 和 ARM64；`unknown/unknown` 的 attestation manifest 是构建证明元数据。

Actions 用仓库自带 `GITHUB_TOKEN` 和 `packages: write`，不需要另存发布 PAT。若同名 Package 的写权限受限，检查 Package 的关联仓库及 Actions access。手动 Run workflow 需要该 workflow 已进入默认分支；当前默认分支为 `dev/michaelhu`，不要仅为发布开发镜像合并整个开发分支。

## 4. 推送方式 B：本地 Buildx 直接推送 GHCR

如果想从本地直接构建并上传，先启动 Docker Desktop 或 OrbStack，确认 `docker info` 成功。使用有 `write:packages` 权限的 GitHub PAT classic 登录；在密码提示处输入 PAT，不要写入文件或命令历史。如组织要求 SSO，需给 PAT 启用组织授权。

```bash
cd /private/var/www/html/ArtisanCloud/X/PowerX/Core/PowerXDocs
docker info
docker login ghcr.io -u YOUR_GITHUB_USERNAME
docker buildx inspect --bootstrap
```

如果当前 builder 不支持多平台，创建一个专用 builder（已存在时跳过创建）：

```bash
docker buildx create --name powerxdoc-publisher --driver docker-container --use
docker buildx inspect --bootstrap
```

为本地工作区选择独立标签，避免覆盖 Actions 管理的版本和 `latest`：

```bash
publish_tag="manual-$(date -u +%Y%m%d-%H%M%S)"
docker buildx build --platform linux/amd64,linux/arm64 \
  --tag "ghcr.io/artisancloud/powerxdoc:$publish_tag" --push .
docker buildx imagetools inspect "ghcr.io/artisancloud/powerxdoc:$publish_tag"
```

预期输出远端 digest 及两个运行平台；记录实际标签。`--push` 已完成上传，单独执行本地 `docker build` 不会推送。多平台构建不需要再 `docker push`。

私有 Package 拉取端只需要 `read:packages`；Public 镜像可匿名拉取。权限及仓库关联规则见 [GitHub GHCR 文档](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。

## 5. 将 XDocker 配置推送到 GitHub

在 XDocker 仓库提交本次编排及指南，不提交 `.env`、证书、私钥或 `data/`：

```bash
cd /private/var/www/html/ArtisanCloud/X/XDocker
git status --short
git add .gitignore .env.example compose.yml readme.md sites nginx certbot scripts tests docs/guides
git commit -m "feat: containerize PowerXDoc hosting with nginx and certbot"
git push origin HEAD
```

推送后服务器才能 clone 或更新这些配置。这一步不构建 PowerXDoc 镜像。

## 6. 在多站点 XDocker 中使用 PowerXDoc

完整公共配置、服务器下载、扩展站点及迁移步骤见 [多站点部署指南](../hosting/README.md)。PowerXDoc 是其中一个独立站点。

```bash
cd /private/var/www/html/ArtisanCloud/X/XDocker
sh scripts/hosting.sh init
```

根目录 `.env` 设置 `ENABLED_SITES="powerxdoc"`、真实 `CERTBOT_EMAIL` 和公共端口。本站配置放在 `sites/powerxdoc/.env`：

```dotenv
SITE_DOMAIN=powerx-doc.artisan-cloud.com
SITE_IMAGE=ghcr.io/artisancloud/powerxdoc:sha-94cb21895e8bf42c45a9bc55fa6f711b7d3d0572
```

将 `SITE_IMAGE` 替换为你实际推送的版本、手动标签或 digest。镜像 Private 时先使用 `read:packages` PAT 登录 GHCR。

本地验证时根目录 `.env` 使用回环地址与 8080/8443 端口：

```bash
sh scripts/hosting.sh compose config --quiet
sh scripts/hosting.sh http powerxdoc
curl -I -H 'Host: powerx-doc.artisan-cloud.com' http://127.0.0.1:8080/
```

本地尚未推送的 `powerxdoc:local` 镜像可填入本站 `SITE_IMAGE`，然后使用 `http powerxdoc --no-pull`。浏览器按多站点指南配置测试域名解析，检查中英文、二级页面刷新、图片、搜索和 Mermaid。

## 7. 服务器 HTTPS 与更新

域名正确指向服务器，公网 80/443 可用。根目录 `.env` 的服务器端口保持 80/443，按顺序执行：

```bash
sh scripts/hosting.sh http powerxdoc
sh scripts/hosting.sh issue-test powerxdoc
sh scripts/hosting.sh issue powerxdoc
sh scripts/hosting.sh https powerxdoc
sh scripts/hosting.sh renew-test powerxdoc
curl -I https://powerx-doc.artisan-cloud.com/
```

先验证网站及公网 ACME 路径，再正式签发。签发会同意 Let's Encrypt 服务条款。本站证书独立存储，HTTPS 配置为 `data/nginx/powerxdoc.conf`；公共 Nginx 与 Certbot 服务可供其他站点复用。

修改 `sites/powerxdoc/.env` 的镜像版本后，只更新本站：

```bash
sh scripts/hosting.sh update powerxdoc
```

回滚恢复上一版本，再执行同一命令。日常整体启动使用 `sh scripts/hosting.sh compose --profile tls up -d`。不要直接在根目录运行 `docker compose up`，否则不会加载网站服务文件。

独立镜像的 GHCR 发布已经验证；多站点配置测试不等于服务器已部署，仍需按公共指南验收容器、真实证书、续期和重启。
