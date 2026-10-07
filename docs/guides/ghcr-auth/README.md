# 私有 GHCR 镜像：服务器登录与拉取

## 1. Private 是支持的部署方式，不必公开镜像

XDocker 的 GHCR 镜像可以保持 **Private**。服务器通过认证后即可拉取，不需要把 Package 改成 Public。本文以 ubuntu 用户通过 sudo 管理 Docker 为默认流程。

| 场景 | 凭证 | 配置位置 |
| --- | --- | --- |
| 本机 SSH 登录服务器 | 本机 SSH 密钥 | 服务器 authorized_keys |
| 服务器 Git clone/pull XDocker | 服务器 SSH Deploy key | GitHub 仓库 Deploy keys |
| 服务器 Docker pull 私有 GHCR 镜像 | GitHub PAT classic，read:packages | 服务器运行 Docker CLI 的用户配置 |
| Actions 发布镜像 | 工作流 GITHUB_TOKEN，packages: write | GitHub Actions 权限 |

SSH 公钥不能用于 Docker registry 登录，Git 克隆成功不代表已经认证 GHCR。GitHub 仓库的公开状态与镜像 Package 的可见性也不是同一个设置。

## 2. 在 GitHub 创建只读拉取 Token

在浏览器登录需要使用的 GitHub 账号：

1. 头像 → **Settings** → **Developer settings**。
2. **Personal access tokens → Tokens (classic)**。
3. 点击 **Generate new token → Generate new token (classic)**。
4. Note 填 `xdocker-server-pull`，选择符合组织要求的有效期。
5. 勾选 **read:packages**，用于下载镜像。
6. 生成后将 Token 保存到自己的密码管理器，下一步在服务器交互输入。

也可直接打开 [创建 classic Token 页面](https://github.com/settings/tokens/new?scopes=read:packages)。目前 GitHub Packages 的个人令牌认证要求 PAT classic，不能用 fine-grained PAT 替代。单纯拉取不需要 write:packages 或 delete:packages。[GHCR 官方说明](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-with-a-personal-access-token-classic)

**scope 和资源权限必须同时具备：** Token 所属账号还必须有目标 Package 的 Read 权限。read:packages 不会让一个没有包访问权限的账号自动获得访问权。

当前镜像分别属于不同组织：

- ReDeployment：xdocker-certbot、xdocker-nginx。
- ArtisanCloud：powerxdoc（其当前公开/私有状态按实际设置判断）。

如果要拉取两个组织的私有包，账号须分别具备对应权限。通过各 Package settings 的 Manage access / Inherited access 检查；需要时由管理员授予只读权限，或核实源仓库继承权限。不要为了拉镜像授予包写入或删除权限。

如果组织使用 SAML SSO，生成 Token 后还要在 Token 列表中 **Configure SSO → Authorize** 对应组织。组织也可能有自己的 Token 访问策略，需要满足该策略。[GitHub SSO 授权说明](https://docs.github.com/en/enterprise-cloud%40latest/authentication/authenticating-with-single-sign-on/authorizing-a-personal-access-token-for-use-with-single-sign-on)

## 3. 在服务器执行 sudo docker login

在 ubuntu 的服务器终端执行，把 YOUR_GITHUB_USERNAME 换成 **PAT 所属的 GitHub 账号登录名**：

```bash
sudo docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

这里不是填写 Linux 的 ubuntu/root 用户名，也不是填写组织名 ReDeployment。除非它本身恰好就是你的 GitHub 登录名。

先遇到 sudo 提示时输入 ubuntu 用户密码；随后 Docker 的 Password 提示处输入 **PAT**，不是 GitHub 账号密码。Token 不写在命令参数、镜像 URL、.env 或聊天中。

成功应显示：

```text
Login Succeeded
```

后续使用 sudo docker / sudo docker compose / sudo sh scripts/certificates.sh，因此此处也必须使用 sudo 登录。普通 `docker login` 保存的是 ubuntu 用户凭证，sudo Docker 默认读取 root 的配置，两者不同；配置了 Docker credential helper 或 DOCKER_CONFIG 时按实际 CLI 配置位置处理。

Docker 登录可能使用凭据存储器；没有配置时会把认证信息编码保存到 Docker 配置文件，而不是加密保护。保持该用户配置目录受限，不要把 config.json 上传仓库或粘贴出来。[Docker login 官方说明](https://docs.docker.com/reference/cli/docker/login/)

## 4. 实际拉取验证，而不只检查登录结果

```bash
sudo docker pull ghcr.io/redeployment/xdocker-certbot:latest
sudo docker pull ghcr.io/redeployment/xdocker-nginx:alpine
```

成功应显示镜像层下载、digest，以及 Downloaded newer image 或 Image is up to date。**Login Succeeded 只证明账号认证成功，实际 pull 成功才证明目标包权限和镜像文件下载通过。**

在 XDocker 中选择这些副本；已经切换成功的不用重复执行：

```bash
cd ~/workspace/XDocker
sh scripts/runtime-images.sh ghcr
sudo docker compose -f compose.yml run --rm --no-deps certbot --version
sudo sh scripts/certificates.sh list
sudo sh scripts/certificates.sh preflight
```

预期依次是 Certbot 版本、REGISTERED 清单、ACME 的 REACHABLE。镜像可见性仍保持 Private，.env 只需保存镜像地址，不能保存 Token。

完整镜像发布及切换说明见 [运行镜像副本指南](../runtime-images/README.md)，证书签发见 [证书管理指南](../certificates/README.md)。

## 5. unauthorized / denied 排查顺序

| 检查 | 正确状态 |
| --- | --- |
| Token 类型 | PAT classic，而不是 fine-grained PAT、SSH 公钥或工作流临时令牌 |
| Token scope | read:packages |
| Token 状态 | 未过期、未撤销，并符合组织访问策略 |
| GitHub 用户名 | PAT 所属账号的登录名 |
| Package 权限 | 该账号具有目标包 Read 权限；需要时已完成组织 SSO 授权 |
| Docker 用户上下文 | 使用 sudo pull 时已经执行 sudo docker login |
| 镜像地址 | ghcr.io/redeployment/xdocker-certbot:latest 等已存在的正确目标 |

需要换 Token 时，按相同用户上下文重新登录：

```bash
sudo docker logout ghcr.io
sudo docker login ghcr.io -u YOUR_GITHUB_USERNAME
sudo docker pull ghcr.io/redeployment/xdocker-certbot:latest
```

不要通过把 .env 改为用户名:Token URL 来绕过认证，也不必因为 unauthorized 自动公开镜像。连接超时是网络阶段的问题；收到 unauthorized 则已进入 registry 认证阶段。两者分开处理。

## 6. Token 更新与边界

Token 过期或撤销会影响后续拉取；不会自动停止已运行的容器。换新 Token 后重新 sudo docker login，验证实际 pull 即可；不用因此重新申请 TLS 证书。

Certbot 续期依赖 ACME 账户数据、域名验证和外网连通，GHCR PAT 只用于镜像下载，不能保证证书签发或续期。保留 Private 的部署只需本文认证步骤，不需要改镜像可见性。

Public 是可选的匿名拉取方式，只有主动希望匿名访问时才在 Package settings 修改可见性；本指南不会修改任何包权限、创建或读取你的 Token。服务器实际登录和拉取仍由你执行并验收。
