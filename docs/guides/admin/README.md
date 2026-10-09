# XDocker 专属管理控制台

专属中文网页管理运行状态：概览、容器服务、网站入口、HTTPS 证书、FRP 穿透及操作记录。它与 Portainer 相互独立，入口不需要 Portainer 账号。

PowerX 的整套部署备份位于“实例备份”：数据库逻辑备份、Redis、配置/密钥与运行文件组合打包，支持私有下载、校验、隔离数据库恢复验证和手动保留清理。详见 [备份管理](../powerx/backups.md)；创建时会暂停所选实例前后端，恢复验证不覆盖运行中的数据库。

“PowerX 实例”按登记实例显示容器及 Setup 连接字段，管理员可确认实例名后显示/复制 PostgreSQL、Redis 密码；见[实例页面指南](../powerx/instances.md)。该页面只读取私有配置，不修改安装状态或数据库密码。

## 实现与管理边界

```text
本机浏览器 127.0.0.1:19080
   → SSH 隧道 → 服务器 127.0.0.1:9080
   → XDocker Admin 容器
       ├── Docker Unix socket：已启用的 XDocker 服务
       ├── 只读配置：站点、域名、证书清单
       ├── 只读报告：自动续期和证书检查
       └── data/admin：访问令牌、登录会话、任务、审计、连接检查
```

- 管理范围包含 `xdocker-web-hosting` 已启用的服务，以及 instances 中明确登记、启用的 PowerX 项目的 backend、web-admin、postgres、redis；过滤未登记项目和临时 Compose 任务。实例服务使用 `powerx-dev/backend` 等完整名称确认操作。
- 容器支持日志、实际 CPU/内存、启动、停止、重启；不提供任意命令、删除容器、删除卷或通用 Docker API 转发。
- 网站与 FRP 支持 HTTPS 连接检查；FRP 使用 `/healthz` 验证整条链路。客户端端口来自公开模板，界面标注“客户端模板”，不当作实际客户端配置。FRPC 密钥和真实私有配置不展示。
- 证书读取真实检查报告、剩余天数及检查时间；支持指定已启用证书的检查、续期测试、按需正式续期。禁止强制续期和新证书签发。正式续期继续使用既有自动 Nginx reload 机制。
- 证书任务用当前续期容器的镜像和固定挂载启动临时 Certbot 容器，不把证书私钥挂到管理服务。任务报告独立，不覆盖定时续期报告。管理服务重启时停止自己登记的未完成临时任务，标记未确认结果。
- 所有变更操作需输入完整目标名称并记录结果；当前一次执行一个任务，重复或并发请求被拒绝。Certbot 自身也会保护证书目录的并发锁，锁冲突作为失败显示，不绕过定时任务。
- 新增站点、修改域名/FRP 映射/数据库配置、镜像升级仍按 [AGENT.md](../../../AGENT.md) 在本地验证、提交、推送，服务器同步同一 SHA。界面中的运行操作保留部署配置和数据；没有远程仓库编辑器。

## 发布镜像

镜像独立于网站：`services/admin/Dockerfile`，Flask API + Gunicorn，静态 HTML/CSS/JS，无外部 CDN。运行代码打入镜像，不在服务器改代码。

修改后先在本地执行：

```bash
python3 -m venv /tmp/xdocker-admin-venv
/tmp/xdocker-admin-venv/bin/pip install -r services/admin/requirements.txt cryptography
/tmp/xdocker-admin-venv/bin/python -m unittest discover -s tests -p 'test_*.py'
node --check services/admin/static/app.js
sh -n scripts/admin.sh scripts/hosting.sh
git diff --check
```

推送 main 后，`Build and Publish XDocker Admin` 工作流先运行回归，再发布 `linux/amd64,linux/arm64` 镜像：

```text
ghcr.io/redeployment/xdocker-admin:sha-<完整提交SHA>
```

工作流随后针对一次性容器验证真实 Docker 状态、日志、资源、停止/启动/重启和退出登录。确认整个工作流成功后再部署对应 SHA 镜像。镜像可保持 Private，认证见 [GHCR 指南](../ghcr-auth/README.md)。

## 服务器启用

先同步已推送提交并确认 SHA，保留现有站点与 FRPS：

```bash
cd ~/workspace/XDocker
git pull --ff-only
git rev-parse HEAD
sh scripts/hosting.sh init
sudo sh scripts/admin.sh init
```

`init` 检测 `.env` 的所有者 UID/GID 和 Docker socket 组，保存到 `services/admin/.env`；创建私有访问令牌，不打印令牌，也不覆盖已有令牌。修改 `.env` 所有者或 Docker socket 组后重新执行 `init`。

编辑 `services/admin/.env`：

```dotenv
ADMIN_IMAGE=ghcr.io/redeployment/xdocker-admin:sha-<成功发布的完整SHA>
ADMIN_PULL_POLICY=missing
ADMIN_PORT=9080
```

在根 `.env` 的 `ENABLED_SERVICES` 中追加 `admin`，保留已有服务，例如 `ENABLED_SERVICES="frps portainer admin"`。

```bash
sudo sh scripts/hosting.sh compose config --quiet
sudo sh scripts/hosting.sh compose pull admin
sudo sh scripts/admin.sh start
sudo sh scripts/admin.sh status
curl -fsS http://127.0.0.1:9080/healthz
```

镜像已经安全导入或下载完成时，可设 `ADMIN_PULL_POLICY=never`。下载不稳定的离线传输方法与 [Portainer 指南](../portainer/README.md) 相同，替换镜像名并导出目标服务器架构。

服务使用部署用户 UID、Docker supplementary group、只读根文件系统、cap_drop 和 no-new-privileges。Docker socket 本身具有服务器级管理能力，只有服务器可信管理员应获得访问令牌。配置和报告只读挂载；不得把令牌、数据库或 `.env` 提交到 Git。

## 打开自己的网页

在 Mac 终端建立隧道，保持终端运行：

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:19080:127.0.0.1:9080 ubuntu@YOUR_SERVER_IP
```

当前服务器可使用 `Tianliyun-shipu` 别名。浏览器打开 **http://127.0.0.1:19080**。VS Code Remote SSH 也可以转发服务器 9080，使用它显示的本机地址。

在服务器终端执行（令牌不发到聊天中）：

```bash
cd ~/workspace/XDocker
sudo sh scripts/admin.sh token
```

将输出粘贴到“管理访问令牌”，点击“连接 XDocker”。登录后令牌输入清空；浏览器只保存 HttpOnly、SameSite=Strict 的随机会话 cookie，八小时过期，不在 localStorage 保存令牌。请求验证 Origin、Host、CSRF，登录失败有次数限制。

### 登录失败：先区分连接与令牌

`Failed to fetch` 表示浏览器请求未完成，不能据此认定令牌错误。SSH 隧道退出后，浏览器可能仍显示已经加载的登录页，但按钮无法连接服务器。关闭终端、电脑休眠或断网后，重新建立隧道并刷新页面。

在 Mac 本机终端测试：

```bash
curl --connect-timeout 3 --max-time 5 http://127.0.0.1:19080/healthz
```

正常返回 `{"status":"ok"}`。连接拒绝或超时时，重开下面的终端并保持运行（当前服务器别名）：

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:19080:127.0.0.1:9080 Tianliyun-shipu
```

若提示本机端口已占用，检查现有隧道或使用 VS Code 转发给出的地址，不启动第二个相同端口隧道。若隧道正常而服务无响应，在服务器执行 `sudo sh scripts/admin.sh status` 和 `curl http://127.0.0.1:9080/healthz`，按实际服务状态处理。

只有服务器明确返回“访问令牌不正确”才重新用 `admin.sh token` 获取当前令牌。PowerX 重新部署或普通 Admin 重启不会替换 `data/admin/access-token`；八小时会话过期需再次登录，令牌本身没有八小时有效期。“请求来源不匹配”需用页面当前地址重新登录，“操作验证已失效”需刷新页面，不通过关闭 Origin/CSRF 校验解决。

## 实际操作

1. **运行概览**：查看容器、站点、证书计数，以及续期容器状态和最近报告时间。
2. **容器服务 → 日志 / 资源**：查看最近 200 行日志、CPU、内存、进程数。常见凭据和 Docker 环境中的敏感值会脱敏；日志仍应由可信管理员查看。
3. **启动 / 停止 / 重启**：阅读影响说明，输入完整服务名，点击“确认执行”。请求提交后转到操作记录；成功意味着 Docker 命令完成且实际运行状态符合预期，仍需检查健康和网站响应。管理服务自身不能从页面停止。
4. **网站入口 → 检查连接**：校验 HTTPS 200，保存最近检查时间与结果。检查所有站点可能需要等待；失败会显示实际失败类型。
5. **HTTPS 证书**：点击检查或续期测试，输入完整证书名。共享 SAN 按证书组执行；点击操作记录查看 Certbot 完整结果。按需续期不会强制重新签发。
6. **FRP 穿透**：查看实际 FRPS 容器状态、客户端模板端口、每个域名最近的端到端检查，必要时查看 FRPS 日志。
7. **操作记录**：区别执行中、成功、失败和服务重启导致的未确认。失败不能当作完成，先检查输出及实际状态。

初次使用建议先检查站点、查看日志和检查一组证书。不要为了试按钮去停止共享 Nginx 或 FRPS。

## 维护

数据保存在 `data/admin/`，目录 700、访问令牌和 SQLite 文件 600。备份前停止管理服务，完整备份该目录；数据库含会话信息，应作为私有数据保存。

```bash
sudo sh scripts/admin.sh stop
# 备份 data/admin/；站点、FRPS、自动续期保持运行。
sudo sh scripts/admin.sh start
```

升级先停止管理服务并备份，发布新镜像、同步仓库、执行 `init` 更新部署 SHA，设置固定镜像 SHA 后启动。回滚恢复相应数据备份和原镜像。令牌泄露时停止管理服务，在服务器私下重新生成访问令牌并清空 SQLite sessions，再启动；不通过网页接受任意文件或命令。

参考：[Docker Engine API](https://docs.docker.com/reference/api/engine/version/v1.44/)、[Flask 安全配置](https://flask.palletsprojects.com/en/stable/web-security/)、[Gunicorn 部署](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/)。
