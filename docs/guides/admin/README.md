# XDocker 专属管理控制台

专属中文网页管理运行状态：概览、容器服务、网站入口、HTTPS 证书、FRP 穿透及操作记录。它与 Portainer 相互独立，入口不需要 Portainer 账号。

PowerX 的整套部署备份位于“实例备份”：数据库逻辑备份、Redis、配置/密钥与运行文件组合打包，支持私有下载、校验、隔离数据库恢复验证和手动保留清理。详见 [备份管理](../powerx/backups.md)；创建时会暂停所选实例前后端，恢复验证不覆盖运行中的数据库。

“PowerX 实例”按登记实例显示容器及 Setup 连接字段，管理员可确认实例名后显示/复制 PostgreSQL、Redis 密码；见[实例页面指南](../powerx/instances.md)。该页面只读取私有配置，不修改安装状态或数据库密码。

“部署前检查”汇总 Docker、配置权限、磁盘、入口端口、GHCR/ACME、DNS 与目标镜像检查。宿主机 Compose 等项目使用终端生成的诊断报告；缺少报告或无法确认私有镜像权限时明确显示未确认。操作步骤和验收范围见[部署前检查指南](preflight.md)。

“备份计划”管理逐实例的全实例维护备份周期、时区和保留清理；运行开关和计划默认禁用，保存不立即备份。启用与自动清理需分别确认，步骤与运行影响见[计划指南](../powerx/backup-plans.md)。此功能的本地实现不代表服务器已经部署或启用。

“镜像版本”分开显示配置引用、实际容器镜像、固定 digest、开发/正式候选与发布证据，并提供只读升级预览。候选目录默认为空，不自动生成或应用版本；见[版本管理指南](../powerx/image-versions.md)。

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
node tests/admin_connection_ui.cjs
sh -n scripts/admin.sh scripts/admin-tunnel.sh scripts/hosting.sh
git diff --check
```

获得提交/推送授权后，使用独立任务分支和 PR，遵守 [AGENT.md](../../../AGENT.md)。`Validate and Publish XDocker Admin` 工作流分为三个阶段：

1. `validate`：Python 回归、前端事件模拟和脚本语法检查。
2. `smoke`：AMD64 使用 `ubuntu-latest`、ARM64 使用 `ubuntu-24.04-arm`，分别原生构建本地候选，**不推镜像**。检查实际 Docker 构建上下文、打包后的 Gunicorn HTTP/认证/静态资源、容器生命周期、完整备份、隔离恢复和一次性计划备份；只使用随机命名的临时容器和可丢弃样例数据。
3. `publish`：两个架构全部验收成功后，接收已测试的 Docker tar 与回执。先校验归档 SHA256、完整源码 SHA、平台和镜像配置 ID，再加载并核对身份；不重新构建。此阶段才登录 GHCR、推送两个架构的镜像并组装索引，最后确认索引包含 AMD64 和 ARM64。

PR 只有读取权限，不上传镜像包、不登录 GHCR、不发布。非 main 分支的手动运行也只做校验。仅原仓库 `ReDeployment/XDocker` 的 main push 或 main 手动运行能进入具有 `packages: write` 权限的发布阶段；同一分支的 push 与手动运行串行执行。部署使用成功工作流产出的完整 SHA 标签：

```text
ghcr.io/redeployment/xdocker-admin:sha-<完整提交SHA>
```

另有 `sha-<完整提交SHA>-amd64`、`sha-<完整提交SHA>-arm64` 两个架构标签；常规部署使用上面的索引标签。导出回执只记录镜像身份、平台和校验值，不含运行配置或令牌；CI 中间制品保留一天。镜像可保持 Private，认证见 [GHCR 指南](../ghcr-auth/README.md)。

在 Actions 中确认 `validate`、两个 `smoke` 和 `publish` 全部成功，再检查 GHCR 索引；只看到 tag 或某一个绿色 job 不代表完整发布成功。若校验/任一 smoke 失败，发布阶段不会执行；若发布阶段失败，可能已有单架构 tag，不能将其当作完整双架构版本部署。失败时查看对应 job 日志并重新验收，不绕过检查手工发布。索引操作参考 [Docker 官方说明](https://docs.docker.com/reference/cli/docker/buildx/imagetools/create/)。工作流代码实现不等于已经在 GitHub 执行或发布。

本地有 Docker 时，可重复运行上述真实验收。以下示例计算当前公共运行文件的内容指纹，明确标记未提交的本地树，不把它冒充为已发布 Git SHA；本地镜像不会推送：

```bash
xdocker_local_revision="local-tree-$(python3 - <<'PY'
from pathlib import Path
import hashlib
root = Path('services/admin')
names = ['.dockerignore', 'Dockerfile', 'requirements.txt', 'app.py', 'docker_api.py',
         'backups.py', 'instances.py', 'preflight.py', 'backup_plans.py', 'image_versions.py',
         'static/index.html', 'static/app.js', 'static/style.css']
digest = hashlib.sha256()
for name in sorted(names):
    digest.update(name.encode() + b'\0' + (root / name).read_bytes() + b'\0')
print(digest.hexdigest())
PY
)"
export ADMIN_SMOKE_IMAGE="xdocker-admin-ci:sha-${xdocker_local_revision}-local"
export ADMIN_SMOKE_REVISION="$xdocker_local_revision"
python3 tests/admin_build_context_smoke.py
docker build --build-arg "REVISION=$ADMIN_SMOKE_REVISION" \
  -t "$ADMIN_SMOKE_IMAGE" services/admin
/tmp/xdocker-admin-venv/bin/python tests/admin_http_smoke.py
/tmp/xdocker-admin-venv/bin/python tests/admin_smoke.py
/tmp/xdocker-admin-venv/bin/python tests/backup_smoke.py
```

脚本清理自己创建的容器和临时数据，保留本地候选镜像。备份验收需要获取 `pgvector/pgvector:pg16`、`redis:7-alpine` 测试依赖，耗时包含测试前后端的正常停机等待。计划验收注入测试时间并单次准入，不启用服务器定时计划；HTTP 检查也不替代浏览器人工验收。

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

### 本机托管隧道与自动重连

以下命令在**本地电脑的 XDocker 仓库**执行，不是在服务器执行。脚本使用系统 Python 3 和 SSH，不读取管理令牌或私钥，不改 `~/.ssh/config`。先在普通终端确认同一个 SSH 别名能使用密钥登录，核对服务器主机身份并完成已有的 known_hosts 登记；脚本采用严格主机验证和非交互认证。需要私钥口令时，先在自己的终端配置 SSH agent，脚本不保存口令。

先用前台方式检查，可同时支持 macOS 和带 Python 3/SSH 的 Linux：

```bash
sh scripts/admin-tunnel.sh run Tianliyun-shipu
```

保持这个终端运行。在另一个**本机终端**查看状态：

```bash
sh scripts/admin-tunnel.sh status
curl --connect-timeout 3 --max-time 5 http://127.0.0.1:19080/healthz
```

成功信号：`supervisor_active: true`、`state: connected`、`current_http: connected`，健康接口返回 `{"status":"ok"}`。网页登录验证是另外一步。前台模式用原终端的 **Ctrl+C** 停止；不会按存储的 PID 杀进程。

macOS 的后台托管使用独立的用户 LaunchAgent，不需要 sudo：

```bash
# 先停止上面的前台 run，然后安装/启动当前仓库的托管任务
sh scripts/admin-tunnel.sh start Tianliyun-shipu
sh scripts/admin-tunnel.sh status

# 卸载并停止本工具的托管任务
sh scripts/admin-tunnel.sh stop
```

`start` 会安装本机任务，登录 macOS 后自动运行；只有你执行或明确授权后才安装。它与 FRPC 的 LaunchAgent 分开。`stop` 验证任务属于当前仓库，只移除这一个任务，不停止其他 SSH 或 FRPC。Linux 当前提供前台 `run`，没有实现 Linux 后台服务安装。

默认本机 `19080` 转发服务器 `9080`。需要独立端口时，启动、查询和停止都指定相同的本机端口：

```bash
sh scripts/admin-tunnel.sh start ubuntu@YOUR_SERVER_IP --local-port 19081 --server-port 9080
sh scripts/admin-tunnel.sh status --local-port 19081
sh scripts/admin-tunnel.sh stop --local-port 19081
```

此时打开 `http://127.0.0.1:19081`。不要同时执行前台和后台启动相同端口。

| 状态 | 含义与处理 |
| --- | --- |
| `reconnecting` | SSH 退出后等待 5、10、20、40、最多 60 秒重试；持续连接 30 秒后重新从 5 秒开始。电脑唤醒、网络恢复后继续尝试。 |
| `port-conflict` | 本机端口被其他进程占用。工具不会接管或关闭已有连接；检查原隧道，或选择其他端口。 |
| `host-key-error` | 主机身份不可信或发生变化。停止任务，人工核对服务器身份和 known_hosts 后重新启动；不自动接受未知主机。 |
| `authentication-error` | SSH 密钥认证失败。停止任务，在普通终端验证同一主机别名与 SSH agent 后重新启动。 |
| `api-unreachable` | SSH 进程可能仍在，但管理 HTTP 服务不可达；核对转发端口及服务器 Admin 状态。 |
| `service-error` / `unexpected-response` | HTTP 5xx 或非预期健康响应；端口有监听不代表管理接口正常。 |
| `stopped` | 没有运行中的本工具托管进程；`current_http` 单独反映端口当前的 HTTP 响应，可能来自已有手动隧道。 |

主机身份、认证、端口冲突会停止重试；`registered: true` 只说明 LaunchAgent 已登记，不代表连接成功。修正这些问题后，执行 `stop` 再 `start`。配置与预定义诊断位于本机 `data/admin-tunnel/<本机端口>/`，目录 700、文件 600；只保存主机别名、端口及状态，不保存令牌、SSH 原始错误输出或私钥。清除状态前先停止连接。

在服务器终端执行（令牌不发到聊天中）：

```bash
cd ~/workspace/XDocker
sudo sh scripts/admin.sh token
```

将输出粘贴到“管理访问令牌”，点击“连接 XDocker”。登录后令牌输入清空；浏览器只保存 HttpOnly、SameSite=Strict 的随机会话 cookie，八小时过期，不在 localStorage 保存令牌。请求验证 Origin、Host、CSRF，登录失败有次数限制。

### 登录失败：先区分连接与令牌

`Failed to fetch` 表示浏览器请求未完成，不能据此认定令牌错误。SSH 隧道退出后，浏览器可能仍显示已经加载的登录页，但按钮无法连接服务器。关闭终端、电脑休眠或断网后，重新建立隧道并刷新页面。

新控制台在登录页及工作区显示管理连接状态、最近成功读取时间和“重新检查连接”。浏览器离线、接口不可达/超时、HTTP 5xx、需要登录（401）与来源/操作验证失败（403）分别提示。连接异常时保留上次运行数据并标明时间，禁用运行操作并隐藏已显示的密码。

页面可见时每 5 秒只读检查；网络恢复后恢复状态读取。**不会自动重发令牌或备份、启停、续期等 POST 操作。** 变更请求发出后失联或超时，结果可能已经在服务器执行：恢复连接后先查“操作记录”和实际服务状态，再决定是否重新执行。403 时点击“重新检查连接”读取当前会话/CSRF；来源地址不匹配时使用当前页面地址重新登录，不关闭安全校验。会话过期后需要手动登录。

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
