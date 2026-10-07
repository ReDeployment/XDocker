# Docker Hub 不可达时使用自有 GHCR 镜像副本

## 1. 故障边界

`failed to resolve reference ... registry-1.docker.io ... i/o timeout` 发生在下载镜像的阶段，此时 Certbot 尚未启动，与域名或证书签发配置无关。

APT 的 Docker CE 镜像源只解决 Docker 安装包下载，不能加速 Docker Hub 的容器镜像。XDocker 提供可选的官方运行镜像副本，服务器可通过 GHCR 拉取，无需修改 Docker 全局网络或安装宿主机 Certbot。

## 2. 已发布的镜像与验证

| 上游官方镜像 | XDocker GHCR 副本 |
| --- | --- |
| `docker.io/certbot/certbot:latest` | `ghcr.io/redeployment/xdocker-certbot:latest` |
| `docker.io/library/nginx:alpine` | `ghcr.io/redeployment/xdocker-nginx:alpine` |

[首次同步 Actions 37591660409](https://github.com/ReDeployment/XDocker/actions/runs/37591660409) 已成功，CI 验证了 Linux AMD64、ARM64 平台存在，并实际启动镜像确认 Certbot 5.8.0、Nginx 1.31.6 和证书清单读取。

首次副本 digest：

- Certbot：`sha256:f70ad0adbb7e117f0fe42a63c553f28ea451edabc0148757b6efcd9735acaa20`。
- Nginx：`sha256:c3be36fe97b0c288e924261a4f89ceaf9c9b368135620ed0b92436014bbf338e`。

副本保留上游运行层，不重新构建业务代码；镜像索引附加来源仓库和上游 digest 注解。移动标签只代表最近一次同步的上游版本，不会自行持续同步上游。生产可在 `.env` 中固定已验证的副本 digest。

## 3. 默认保持 Private，通过 PAT 认证拉取

**镜像不必改成 Public。** Private 是支持的部署方式，按 [私有 GHCR 登录指南](../ghcr-auth/README.md) 创建 PAT classic、勾选 read:packages，并确认该账号对目标包有 Read 权限；需要 SSO 时授权对应组织。

服务器后续使用 sudo Docker，因此先执行：

```bash
sudo docker login ghcr.io -u YOUR_GITHUB_USERNAME
sudo docker pull ghcr.io/redeployment/xdocker-certbot:latest
sudo docker pull ghcr.io/redeployment/xdocker-nginx:alpine
```

Docker 的 Password 提示处输入 PAT，用户名填 PAT 所属的 GitHub 账号名；不是 Linux 用户名、组织名或 GitHub 账号密码。SSH Deploy key 仅用于 Git，不能用于 registry 登录。普通 docker login 与 sudo docker login 是不同的用户配置，不能混用。

成功应看到 Login Succeeded，并完成实际 pull。新包匿名访问返回 401 是 Private 的预期，不代表镜像没有发布。需要主动提供匿名下载时才选择 Public，不是部署前提。

可选 Public 设置：在 [xdocker-certbot](https://github.com/orgs/ReDeployment/packages/container/package/xdocker-certbot) 和 [xdocker-nginx](https://github.com/orgs/ReDeployment/packages/container/package/xdocker-nginx) 的 Package settings → Change visibility 中选择 Public。保持 Private 时跳过此操作。

## 4. 切换服务器的现有 .env

```bash
cd ~/workspace/XDocker
git pull --ff-only
sh scripts/runtime-images.sh ghcr
```

这一步只修改 `.env` 的 NGINX_IMAGE 与 CERTBOT_IMAGE，其他域名、邮箱、端口等设置保留。原配置备份到 Git 忽略的 `data/backups/`。不重启运行服务，也不修改证书。

只执行 git pull 不会覆盖已经存在的 `.env`，所以必须切换配置；否则 Certbot 仍会访问 Docker Hub。

然后验证：

```bash
sudo docker compose -f compose.yml pull certbot nginx
sudo docker compose -f compose.yml run --rm --no-deps certbot --version
sudo sh scripts/certificates.sh list
sudo sh scripts/certificates.sh preflight
```

预期拉取地址为 ghcr.io/redeployment，Certbot 版本可读，清单出现 REGISTERED，ACME API 为 REACHABLE。这里没有申请证书。只验证 Certbot 时可以先单独执行 `pull certbot`。

- 超时：检查服务器访问 GHCR token 与镜像层下载地址的实际网络，接口 401 不等于完整镜像可下载。
- unauthorized/denied：按私有登录指南检查 PAT classic、read:packages、包 Read 权限、组织 SSO，以及 sudo Docker 的登录上下文；不用自动改 Public。
- 仍显示 docker.io：检查 `.env` 中两项 IMAGE 值及当前目录。

## 5. 更新副本或恢复上游

仓库工作流 `.github/workflows/mirror-runtime-images.yml` 首次由配置提交触发，也支持手动运行。在 [Actions](https://github.com/ReDeployment/XDocker/actions/workflows/mirror-runtime-images.yml) 点击 Run workflow，等待 Copy 与 Run mirrored images 两步都成功，再记录新 digest 并在服务器验证拉取。

同步通过 GitHub Actions 的 GITHUB_TOKEN 写入本仓库的 GHCR Package。上游标签先解析为固定 digest，再复制完整多平台索引；没有构建一个包含网站、Nginx、Certbot 的混合镜像。[Docker 官方跨仓库复制方式](https://docs.docker.com/build/ci/github-actions/copy-image-registries/)

Docker Hub 线路恢复后，可切回官方上游地址：

```bash
sh scripts/runtime-images.sh upstream
```

配置切换与实际服务更新是两步：需要更新已运行的入口或续期容器时，再拉取镜像并按部署指南重新创建相应服务。保留 data/ 中的证书和业务配置。

## 6. 当前验收范围

Actions 发布、多平台索引检查及 CI 实际运行已完成。当前副本允许保持 Private，匿名访问拒绝认证是预期。尚未代创建 Token 或修改 Package 可见性。目标服务器的副本拉取、Certbot 容器和 ACME preflight 仍需回传验证。
