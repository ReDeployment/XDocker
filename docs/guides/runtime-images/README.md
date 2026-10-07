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

## 3. 先解决副本的拉取权限

新 GHCR Package 默认 Private，首次发布后的匿名检查返回 401。想无需 Token 拉取，可把这两份公开上游镜像副本改成 Public：

1. 打开 [xdocker-certbot](https://github.com/orgs/ReDeployment/packages/container/package/xdocker-certbot) 和 [xdocker-nginx](https://github.com/orgs/ReDeployment/packages/container/package/xdocker-nginx)。
2. 进入各自的 **Package settings**。
3. 找到 **Change visibility**，选择 **Public**，按页面要求输入名称确认。
4. Public 后服务器无需 Docker login；仍需实际拉镜像确认网络。

如果希望保留 Private，使用有 `read:packages` 的 GitHub PAT classic 登录。后续命令使用 sudo，因此也要用 sudo 登录，避免凭证只保存在 ubuntu 用户而 Docker 命令读取 root 配置：

```bash
sudo docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

在交互密码提示输入 PAT，不把 Token 写进仓库、镜像地址或聊天。GitHub SSH Deploy key 仅用于 Git 仓库，不能用于 Docker registry 认证。[GHCR 官方说明](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)

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
- unauthorized/denied：检查 Package 是否 Public，或者 sudo Docker 的 PAT 登录是否成功。
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

Actions 发布、多平台索引检查及 CI 实际运行已完成。首次匿名访问仍需要 Package Public 或认证；浏览器连接不可用，代理未代改可见性。目标服务器的副本拉取、Certbot 容器和 ACME preflight 仍需回传验证。
