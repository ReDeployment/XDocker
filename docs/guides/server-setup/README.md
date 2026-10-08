# 新服务器准备：Ubuntu 用户、SSH、VS Code、Git 与 Docker

这份指南是 XDocker 的第一份操作指南。从新 Ubuntu 服务器开始，完成登录、下载部署配置、安装 Docker，再验证 Certbot 容器。

已完成且验证通过的阶段直接跳过，不要为阅读新指南重复创建用户、覆盖密钥或重装 Docker。新服务器的主流程不依赖旧服务器，不要求复制旧证书，也不在宿主机安装 Nginx、Certbot 或 Node。

## 1. 终端与示例约定

| 标记 | 在哪里执行 | 用途 |
| --- | --- | --- |
| 本机终端 | 你的 Mac | 保存本机私钥、SSH 登录、VS Code |
| 服务器 root | 初始管理会话 | 创建 ubuntu 用户、配置 SSH 服务 |
| 服务器 ubuntu | 后续日常会话 | Git、`~/workspace/XDocker`、通过 sudo 管理 Docker |

示例 `SERVER_IP` 与 SSH 端口 `22` 必须替换为实际连接地址和端口。本机示例密钥为 `~/.ssh/ArtisanCloud`；如果你使用其他密钥文件，替换所有相关路径。服务器上不需要保存这把本机私钥。

先在服务器检查系统：

```bash
cat /etc/os-release
uname -m
```

本指南 Docker 安装命令按本次机器的 **Ubuntu 22.04 / jammy / x86_64（APT 架构 amd64）** 编写。其他版本按 Docker 官方文档调整发行版与架构。

## 2. 验证出站网络

在服务器执行：

```bash
curl -4 -sS -D - -o /dev/null --connect-timeout 10 --max-time 20 https://github.com
curl -4 -sS -D - -o /dev/null --connect-timeout 10 --max-time 20 https://ghcr.io/v2/
curl -4 -sS -D - -o /dev/null --connect-timeout 10 --max-time 20 https://registry-1.docker.io/v2/
curl -4 -I --connect-timeout 10 --max-time 20 https://download.docker.com/linux/ubuntu/gpg
curl -4 --connect-timeout 10 --max-time 20 https://acme-v02.api.letsencrypt.org/directory
curl -4 --connect-timeout 10 --max-time 20 https://acme-staging-v02.api.letsencrypt.org/directory
```

| 检查 | 预期结果 | 实际证明 |
| --- | --- | --- |
| GitHub | HTTP 响应，例如 200/重定向 | HTTPS 接口可达；SSH 还要单独测 |
| GHCR / Docker Hub | GET 返回 200 或 401 | Registry 接口可达；实际拉镜像仍待验证 |
| Docker GPG 地址 | HTTP 200 | 官方安装源入口可达；大软件包下载速度仍待观察 |
| 两个 ACME directory | JSON 包含 newNonce/newAccount/newOrder | Certbot 所需 API 可达；域名验证尚未完成 |

`curl -I` 是 HEAD 请求，部分 Registry 返回 405；收到 HTTP 响应说明接口可达，可改用上面的 GET 检查。超时尚未进入密钥/账号验证阶段，不能靠修改 public key 修复。Cloudflare 官网可达也不能推断 GitHub 或 Docker Hub 可达。

签发还需要域名验证请求从公网到达服务器的 80 端口，这与本节出站检查是两项验收。

## 3. 创建 ubuntu 用户

在已有的服务器 root 会话执行：

```bash
id ubuntu >/dev/null 2>&1 || adduser ubuntu
usermod -aG sudo ubuntu
install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
touch /home/ubuntu/.ssh/authorized_keys
chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys
chmod 600 /home/ubuntu/.ssh/authorized_keys
id ubuntu
```

`adduser` 的密码由你在服务器终端设置，供 sudo 使用；个人资料项可以回车使用默认值。预期 `id ubuntu` 显示 ubuntu 及 sudo 组。Ubuntu 官方操作参考：[用户管理](https://ubuntu.com/server/docs/how-to/security/user-management/)。

## 4. 配置本机 → 服务器的公钥登录

本机终端读取公钥：

```bash
cat ~/.ssh/ArtisanCloud.pub
```

服务器 root 终端编辑：

```bash
nano /home/ubuntu/.ssh/authorized_keys
```

追加公钥完整的一行，保留已有条目。保存后重新检查 owner 和权限：

```bash
chown ubuntu:ubuntu /home/ubuntu/.ssh /home/ubuntu/.ssh/authorized_keys
chmod 700 /home/ubuntu/.ssh
chmod 600 /home/ubuntu/.ssh/authorized_keys
```

本机另开一个终端，用指定密钥验证：

```bash
ssh -p 22 -i ~/.ssh/ArtisanCloud -o IdentitiesOnly=yes -o PreferredAuthentications=publickey ubuntu@SERVER_IP
```

预期进入 ubuntu 会话，`whoami` 为 ubuntu。第一次连接需核对服务器主机指纹。不要在尚未验证新连接时关闭原 root 会话。

SSH 密钥登录免的是服务器登录密码；本地私钥如果有口令，仍可能提示 `Enter passphrase for key`。`sudo` 仍需 ubuntu 用户密码，这三者是独立的。

### 4.1 按需允许 root 使用公钥登录

需要 root SSH 时，先在 root 的 `authorized_keys` 追加同一份本机公钥：

```bash
install -d -m 700 -o root -g root /root/.ssh
touch /root/.ssh/authorized_keys
nano /root/.ssh/authorized_keys
chown root:root /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

只追加你希望授权给 root 的公钥，不覆盖已有内容。

### 4.2 分阶段关闭 SSH 密码登录

以下为首次配置流程；如果两个账号已经实现密钥登录且服务已配置 key-only，跳过这段初始化，直接验证有效配置和新连接。若同名配置文件已存在，先查看、备份并编辑已有设置，不覆盖成阶段性默认值。

首次配置时，服务器 root 终端执行：

```bash
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/00-00-xdocker-key-login.conf <<'EOF'
PubkeyAuthentication yes
PermitRootLogin prohibit-password
EOF
/usr/sbin/sshd -t && systemctl reload ssh
```

`prohibit-password` 允许 root 公钥认证并禁止 root 密码认证。保留原会话，本机新终端分别测试 ubuntu 和 root：

```bash
ssh -p 22 -i ~/.ssh/ArtisanCloud -o IdentitiesOnly=yes -o PreferredAuthentications=publickey ubuntu@SERVER_IP
ssh -p 22 -i ~/.ssh/ArtisanCloud -o IdentitiesOnly=yes -o PreferredAuthentications=publickey root@SERVER_IP
```

两者成功后，再在 root 终端追加全局 key-only 设置：

```bash
cat >> /etc/ssh/sshd_config.d/00-00-xdocker-key-login.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
EOF
/usr/sbin/sshd -t
/usr/sbin/sshd -T | grep -E '^(pubkeyauthentication|permitrootlogin|passwordauthentication|kbdinteractiveauthentication|authenticationmethods) '
```

期望 PubkeyAuthentication=yes、PermitRootLogin=prohibit-password（也可能显示 without-password）、PasswordAuthentication=no、KbdInteractiveAuthentication=no、AuthenticationMethods=publickey。若有效值不符，先检查主配置、Include 顺序及 Match 条件，不继续切断旧登录路径。OpenSSH 通常使用首次读取的值。

检查通过后 `systemctl reload ssh`，再用新终端验证两个账号。若配置错误，在保留的管理会话中恢复之前的文件或编辑有问题的配置，`sshd -t` 通过后 reload；云控制台作为恢复入口。[Ubuntu OpenSSH 配置说明](https://ubuntu.com/server/docs/how-to/security/openssh-server/)

## 5. VS Code Remote-SSH

在本机安装 Microsoft 的 Remote-SSH 扩展。编辑本机 `~/.ssh/config`，新增独立别名，保留已有配置：

```sshconfig
Host xdocker-server
    HostName SERVER_IP
    User ubuntu
    Port 22
    IdentityFile ~/.ssh/ArtisanCloud
    IdentitiesOnly yes
```

本机先 `ssh xdocker-server` 确认登录，再在 VS Code：

1. `Cmd+Shift+P` → `Remote-SSH: Connect to Host...`。
2. 选择 `xdocker-server`，首次选择远端 Linux。
3. 等待 VS Code Server 安装完成，打开 `/home/ubuntu/workspace/XDocker`（第 6 节下载后）。
4. 在远端终端执行 `whoami` 和 `pwd`，应为 ubuntu 及所选服务器目录。

SSH 连接成功不替代 VS Code Server 安装验收。安装失败时查看 Remote-SSH 输出中的实际错误和下载地址。[VS Code 官方连接步骤](https://code.visualstudio.com/docs/remote/ssh)

### 5.1 macOS 的 Cmd+K 不再清理终端

扩展可能覆盖默认快捷键。打开本机 VS Code 的 Preferences: Open Keyboard Shortcuts (JSON)，在现有数组末尾追加下列规则，并保留原来的其他绑定：

```json
{
  "key": "cmd+k",
  "command": "workbench.action.terminal.clear",
  "when": "terminalFocus"
}
```

保存后点进集成终端，再按 Cmd+K 验证；该规则只在终端有焦点时触发。若仍无效，用 Developer: Toggle Keyboard Shortcuts Troubleshooting 记录实际命中的规则；文件修改成功不替代运行中的按键验证。[VS Code 终端快捷键说明](https://code.visualstudio.com/docs/terminal/advanced#macos-clear-screen)

## 6. 配置服务器 → GitHub 的独立密钥，并拉取 XDocker

两段连接不同：本机公钥放在服务器 authorized_keys；服务器访问 GitHub 的公钥放在 GitHub。服务器上创建独立 Deploy key，不复制本机私钥。

服务器 ubuntu 终端执行；已有同名文件时先检查并复用，不覆盖：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/xdocker_github -C "ubuntu-xdocker"
cat ~/.ssh/xdocker_github.pub
```

在 [XDocker → Settings → Deploy keys](https://github.com/ReDeployment/XDocker/settings/keys) 点击 Add deploy key，Title 填 ubuntu-xdocker，Key 粘贴该 `.pub` 完整内容；只拉取不勾选 Allow write access。Key already in use 表示已绑定其他账号或仓库，先核实旧绑定，或生成本仓库独立密钥。[GitHub Deploy key 说明](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)

明确指定这个非默认文件名测试：

```bash
ssh -T -i ~/.ssh/xdocker_github -o IdentitiesOnly=yes -o ConnectTimeout=10 git@github.com
```

预期 `Hi ... You've successfully authenticated`；GitHub 不提供 shell，测试命令可能返回退出码 1，不应仅凭这个退出码判断认证失败。首次连接核对 [GitHub 主机指纹](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)。

测试成功后：

```bash
mkdir -p ~/workspace
cd ~/workspace
GIT_SSH_COMMAND='ssh -i ~/.ssh/xdocker_github -o IdentitiesOnly=yes'   git clone git@github.com:ReDeployment/XDocker.git
cd XDocker
git config core.sshCommand 'ssh -i ~/.ssh/xdocker_github -o IdentitiesOnly=yes'
git log -1 --oneline
```

不要使用 sudo git clone，否则会使用 root 的密钥和 home，并可能把部署目录创建成 root 所有。仓库已存在时检查目录内容，不重复 clone 或删除它；已有 Git checkout 后续用 `git pull --ff-only`，本地修改和分支分歧先处理，不强制覆盖。

### 6.1 22 端口超时时，单独测试 SSH 443

```bash
ssh -T -p 443 -i ~/.ssh/xdocker_github -o IdentitiesOnly=yes -o ConnectTimeout=10 git@ssh.github.com
```

若成功，把 clone 地址改为 `ssh://git@ssh.github.com:443/ReDeployment/XDocker.git`，继续显式指定密钥。仍超时则处理网络；Permission denied (publickey) 则检查密钥选择和 GitHub 公钥绑定。[GitHub SSH 443 文档](https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port)

ghproxy 一类 HTTPS 下载加速器不能代替 SSH 代理。SSH 两条线路均不通时，可用本机上传 Git 快照包；快照不含 `.git`，后续不能直接 git pull。镜像域名可能变动，使用前核实供应商推荐地址；GitHub 下载代理与 Docker 镜像仓库也不是同一项配置。

## 7. 安装 Docker Engine、Buildx、Compose

先检查：

```bash
sudo docker version
sudo docker compose version
```

如果 Client/Server 与 Compose 都正常，直接跳到第 9 节。若 command not found，继续下面安装。本段按 Ubuntu 22.04 AMD64 配置；从容器化主机迁移时，不执行不加区分的重装或卸载。

以下完整安装块包括 GPG key、APT 软件源、刷新索引、安装与验证，不要只复制最后的 apt install。括号中的 set -e 在失败时停止该安装块，并返回原终端。

```bash
(
set -e
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -4 -fsSL --connect-timeout 10 --max-time 60   https://download.docker.com/linux/ubuntu/gpg   -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: jammy
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y --no-remove   docker-ce docker-ce-cli containerd.io   docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
sudo docker buildx version
)
```

预期 apt update 包含 Docker 源的 jammy InRelease，安装结束后 docker version 同时显示 Client 和 Server，Compose 显示版本号。--no-remove 拒绝安装期间移除现有包；遇到冲突时先核实原包用途，不盲目卸载已有 containerd/runc。[Docker 官方 Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/)

本指南后续 Docker 操作使用 sudo，无需先授予 docker 组权限。SSH 免密码不意味着 sudo 免密码；sudo 仍使用创建 ubuntu 时设置的密码。

### 7.1 包找不到：检查软件源，不重复安装

```bash
cat /etc/apt/sources.list.d/docker.sources
sudo apt update
apt-cache policy docker-ce
```

若 apt update 只有 archive.ubuntu.com/security.ubuntu.com，没有 Docker 源，先补齐上一节 GPG key 和 docker.sources。安装失败后 docker.service 不存在是后续现象，不应先反复 systemctl enable。

### 7.2 下载很慢：更换 Docker CE 安装包源

下载阶段若只有十几 kB/s，可 Ctrl+C 等待 apt 返回提示符，再切换中国科大镜像。已经进入 dpkg 安装/配置阶段时不要按下载流程中断。保留缓存，不执行 apt clean，也不删除 APT 锁文件。

```bash
(
set -e
sudo cp -n /etc/apt/sources.list.d/docker.sources   /etc/apt/docker.sources.official-backup
sudo sed -i   's|https://download.docker.com/linux/ubuntu|https://mirrors.ustc.edu.cn/docker-ce/linux/ubuntu|'   /etc/apt/sources.list.d/docker.sources
sudo apt -o Acquire::ForceIPv4=true update
sudo apt -o Acquire::ForceIPv4=true install -y --no-remove   docker-ce docker-ce-cli containerd.io   docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
)
```

继续使用原 Docker 官方 GPG key，速度以实际服务器输出为准。需要恢复时把备份复制回 docker.sources，再刷新 apt 索引。[科大 Docker CE 镜像帮助](https://mirrors.ustc.edu.cn/help/docker-ce.html)

**Docker CE 的 APT 镜像只加速安装包，不等于 Docker Hub 的容器镜像加速。** Docker 已安装也不代表 Certbot、Nginx 或 GHCR 镜像已经能下载。

## 8. 初始化 XDocker 配置

服务器 ubuntu 终端执行：

```bash
cd ~/workspace/XDocker
sh scripts/hosting.sh init
```

出现下面的提示就是初始化完成：

```text
Edit .env and sites/<site>/.env. Then run: sh scripts/hosting.sh http <site>
```

`init` 复制缺失的根 `.env` 和各站点 `.env`，创建 data 目录及默认入口配置，保留现有文件。它不会安装 Docker、拉取镜像、启动网站或申请证书。提示中的 http 是网站启动入口；如果本阶段先验证 Certbot，继续第 9 节即可。

## 9. 第一阶段验证 Certbot 容器

使用私有 GHCR 副本时，先按 [认证指南](../ghcr-auth/README.md) 创建有 read:packages 且能读取该包的 PAT classic，然后 `sudo docker login ghcr.io -u YOUR_GITHUB_USERNAME`。Package 保持 Private 即可，SSH 密钥认证 Git 成功不会替代这一步。登录成功后实际 pull 验证；已有 .env 的镜像切换仍使用运行镜像指南。

```bash
cd ~/workspace/XDocker
sudo docker compose -f compose.yml run --rm --no-deps certbot --version
sudo sh scripts/certificates.sh list
sudo sh scripts/certificates.sh preflight
```

| 命令 | 预期输出 | 验收含义 |
| --- | --- | --- |
| certbot --version | Certbot 版本号 | 镜像已实际下载，容器可运行 |
| list | REGISTERED，默认 8 份证书/10 个域名 | 挂载脚本与清单可读 |
| preflight | REACHABLE 和 ACME directory URL | 容器内能访问证书 API |

如果第一条镜像拉取超时，按 [自有 GHCR 镜像副本指南](../runtime-images/README.md) 完成 Private 镜像的 sudo Docker/PAT 认证并执行 `sh scripts/runtime-images.sh ghcr` 切换现有 .env，再重试版本验证，不把下载错误解释成域名或证书配置错误。新服务器尚未签发时，check 报缺少证书是预期；第一阶段先运行 list/preflight。API 连通之后还需要 Nginx 挑战路径、DNS 和 dry-run 验证。

接下来先编辑根 `.env` 的真实 CERTBOT_EMAIL，选择本机负责的证书项和测试域名，再按 [新服务器证书操作](../certificates/README.md) 完成挑战路径、测试签发、正式签发及公网 TLS 验证。业务网站部署见 [多站点指南](../hosting/README.md)，PowerXDoc 的镜像发布见 [PowerXDoc 指南](../powerxdoc/README.md)。

## 10. 本次服务器的已确认状态与剩余验收

以下是 2026-10-07 用户回传及截至 2026-10-08 通过 SSH 和公网请求在新服务器 SYG962131 实际验证的结果，不代表其他站点自动通过：

- 系统：Ubuntu 22.04 LTS / x86_64。
- SSH：用户已确认账号和密钥登录成功；GitHub SSH 克隆成功。最终 sshd 有效配置未提供完整回传。
- Docker：Client/Server 都为 29.8.2，containerd 2.3.6，Compose 5.6.0，服务已启动。
- XDocker init：已输出正常初始化提示。
- Docker Hub 直连超时；GHCR 大镜像层下载缓慢且连接 reset，原始在线 pull 未完整成功。
- 已从相同 AMD64 manifest 的官方上游在本机导出，经 SSH 上传、校验 SHA256 后导入 Certbot 和 Nginx；GHCR 包仍保持 Private。
- 服务器实际输出 Certbot 5.8.0、Nginx 1.31.6，8 份证书/10 个域名的清单可读，容器内 ACME 生产 API 返回 REACHABLE。
- 服务器 .env 已备份并设置 CERTBOT_PULL_POLICY=never、NGINX_PULL_POLICY=never，日常使用完整本地缓存；升级先显式拉取或导入。
- PowerXDoc：已导入发布镜像的 AMD64 版本，网站容器健康；共享 Nginx 已启动并监听 80/443。
- `powerx-doc.artisan-cloud.com` 已解析到 `160.202.238.184`。供应商处理公网访问后，首页和 HTTP-01 挑战路径均可从外部访问。测试签发首次遇到二次验证连接超时，重试通过后才正式签发。
- 已为 PowerXDoc 正式签发 Let's Encrypt 证书，有效期至 `2027-01-06T03:19:39+00:00`；公网 HTTP 301 跳转 HTTPS，HTTPS 首页、产品概览和静态资源均返回 200，未知路径返回 404，TLS 信任、域名及本地证书匹配检查通过。
- PowerWechat：源码仓库已发布经过 Actions 运行检查的 AMD64/ARM64 镜像，服务器已部署健康的独立站点容器；用户将 `powerwechat.artisan-cloud.com` 切到新 IP 后，测试签发、正式签发、公网 HTTPS、文档及资源检查、续期 dry-run 均通过，证书有效期至 `2027-01-06T04:29:07+00:00`。详细记录见 [PowerWechat 指南](../powerwechat/README.md)。
- 本机使用被 Git 忽略的 `certbot/certificates.local.json`，启用 PowerXDoc 和 PowerWechat；根 `.env` 配置真实证书联系邮箱。其他 6 组证书尚未迁移或签发。
- `certbot-renew` 已启动，正常检查报告 `LOCAL_VALID`，新证书未到续期窗口时报告 `UNCHANGED`；PowerXDoc 续期 dry-run 成功。
- 真实到期续期后的自动 reload、服务器重启恢复和其他站点：尚待验收；本次没有强制续期或重启服务器。

复用本指南到其他服务器时重新验证各项网络和运行状态，不把本次日志当作新机器的验收结果。
