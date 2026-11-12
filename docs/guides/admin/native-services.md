# 独立项目接入与原生 XDocker

更新：2026-11-12。项目保留自己的开发、构建及启动方式；XDocker 接入现有地址、监控分组、网页入口与明确授权的常驻服务。保存登记不会运行项目命令，不生成项目 Dockerfile，也不接管其他项目的部署仓库。

## 原生开发入口

XDocker Admin 可以直接运行于 Python/Gunicorn。无需 Docker 构建，也可完全不连接 Docker。先在本地 XDocker checkout 安装依赖并准备独立开发状态：

```sh
python3 -m venv .venv
.venv/bin/pip install -r services/admin/requirements.txt
.venv/bin/python scripts/admin-native.py init --state data/native-admin
.venv/bin/python scripts/admin-native.py serve --state data/native-admin --port 19081 --reload --without-docker
```

打开 `http://127.0.0.1:19081/#service-hub`。令牌只在自己的终端通过 `scripts/admin-native.py token --state data/native-admin` 读取，勿提交或贴到日志。修改前端刷新浏览器，Python 由 Gunicorn reload；依赖改变再更新 venv。没有开启交互式调试器。

状态目录及令牌为 0700/0600，带 checkout 身份标记，不能指向已存在的 Docker Admin 数据库或其他 checkout。原生实例关闭旧的容器任务恢复、备份与观察后台调度，Docker 不可达仍可登录、登记、读指标和入口状态。旧的容器运行操作不从这个原生实例开放。

Ubuntu 长期运行可使用 `serve --collect`，此时每 15 秒的指标采集由 web worker 之外的监督进程运行，重载不会重复启动采集器；需要观察容器时省略 `--without-docker`。macOS 不伪造 Linux 指标，`--collect` 仅在 Linux 接受。原生进程的资源口径及容量仍遵循 [应用资源监控](application-resources.md)。

原生启动不改变仓库的远程开发规则：代码仍在本地修改与验证，获得指定分支推送授权后，服务器同步准确 SHA 再运行；不得把远程临时编辑当成最终修复。

## 登记顺序

进入「系统管理 → 独立服务」，依次使用接入概览、项目登记、网页入口、常驻服务、操作记录。桌面左侧纵向导航，窄屏横向任务导航；只显示当前任务，编辑在弹窗，切换/刷新保留草稿。绑定失效、运行结果未确认及网关回退异常在所有分区外持续显示。

1. **项目自行启动**：由项目维护者使用自己的开发命令或正式部署流程，确认实际设备、监听地址与端口。目录中存在 Git checkout 不代表已经启动，也不能把文档内的采集副本当运行实例。
2. **主机管理员批准接入地址**：在私有 policy 中登记别名、固定 IP/协议/端口、健康路径和预期 HTTP 状态。HTTP API 只选择已有别名，不接受任意 URL、shell 或 unit。域名不用于后端探测解析；探测不使用系统代理、不跟随重定向、不返回业务响应正文。
3. **保存项目登记**：选择别名、访问域名、可选资源分组及常驻服务，记录项目交付方式。启用“纳入下一次网关配置”仅影响待应用配置，不立即改变流量或进程。
4. **检查连接与资源**：检查接入地址，再在「应用资源」选择组件建立分组，用分组标识关联项目。健康结果 30 秒有效，绑定/策略变化后失效。进程存在、HTTP 可达、业务验收是不同结论。
5. **预览/应用网页入口**：核对域名解析、Nginx 监听地址/端口和完整配置，然后明确确认 `xdocker-gateway`。服务继续使用原启动方式。

可信策略示例及项目示例在 [native-services](../../examples/native-services/)。将它们复制到私有目录、替换为真实值，文件设为 0600。原生 init 创建 revision=1 的空策略，首次导入用 revision=2，后续必须加一：

```sh
.venv/bin/python scripts/service-hub.py policy-import --state data/native-admin --file /absolute/private/policy.json
.venv/bin/python scripts/service-hub.py project-register --state data/native-admin --file /absolute/private/project.json
.venv/bin/python scripts/service-hub.py gateway-preview --state data/native-admin
```

policy-import 仅为受信 CLI，不提供网页上传策略入口。JSON 文件用于导入，生效数据写入 SQLite。项目目录仅用于识别，不作为执行或文件读取入口。项目修改也使用 revision+1，同域名和同服务的重复归属被拒绝；与现有已启用 XDocker 网站域名冲突时阻断。监听为通配地址时也会检查转发回环。最多 100 个项目/地址/服务/证书引用。

## SQLite 持久化

`<state>/registry.sqlite3`（0600）保存可信接入地址、服务授权、证书引用、网关设置、项目登记、监控分组与组件绑定、操作回执及实际网关版本。它使用明确的关系表、唯一约束和事务；项目保存、操作意图及结果均先提交数据库。`admin.sqlite3` 继续承载原有 Admin 会话/审计，`host-metrics/history.sqlite3` 承载有界指标历史，避免互相迁移已有数据。Nginx 的实际配置与 pid 仍是运行文件。

旧版 `service-hub/policy.json`、`service-hub/projects.json`、`resources/apps.json` 及网关回执在首次启动时事务迁入。升级前停止使用同一 state 的旧 Admin；迁移保留原文件，并在 `registry-migration/` 保存 SHA-256 对应的私有备份与数据库内收据。损坏数据、重复域名/组件、未知 schema 或迁移后被旧程序改写的 JSON 都阻断启动；不会回退到空配置或双写 JSON。修复或恢复前应保留整份 state 并核对收据。

```sh
.venv/bin/python scripts/service-hub.py registry-status --state data/native-admin
.venv/bin/python scripts/service-hub.py registry-backup --state data/native-admin --file /absolute/private/backups/registry-20261112.sqlite3
```

备份使用 SQLite backup API，可获得一致的数据库副本；目标必须不存在。恢复需要停止 Admin，并连同原迁移收据/源文件核对，不能在线覆盖数据库或只复制正在写入的文件。

交付方式 `external/source/binary/image` 分别记录自行部署、源码构建、二进制或镜像。当前不提供通用“部署”按钮：构建、安装、数据库迁移及版本回退须由对应项目交付明确合同，不能把保存这个字段宣布为已部署。XDocker 的固定入口、健康检查及受控服务操作可在项目完成交付后使用。

## Nginx 原生网关

首版执行器面向 Linux 宿主机 Nginx，使用 `<state>/service-hub/gateway/` 独立 prefix、pid、日志和配置，不改 `/etc/nginx`，不接管已有 Nginx。原有容器网站/FRP 路径继续使用既有 hosting 功能；容器中的 127.0.0.1 不能当作宿主机地址。

网关默认禁用。需要启动它时，主机须已安装 Nginx，在私有 policy 中显式配置 enabled、固定可执行文件、监听 IP 与高位端口（1024–65535）；原生 Admin 启动时再明确加 `--operations`。`--reload` 与 `--operations` 不可同时使用。主机权限不会被自动扩大，不会自动 sudo 或安装系统软件。

默认 HTTP 网关访问形式为 `http://example.home.test:8088`，HTTPS 为配置的 8443 端口。应用 plan 已核对后，项目/入口列表提供可点击链接；链接存在不代表 DNS/TLS 或业务验收已完成。域名转发到原地址，保留路径，显式传递 Host、来源协议和 WebSocket Upgrade/Connection，支持开发 HMR；不会关闭项目框架自身的 Host 检查。参考 [Nginx WebSocket 文档](https://nginx.org/en/docs/http/websocket.html)。目标为 HTTPS 时验证上游证书，Ubuntu 使用系统 CA bundle。

TLS 可选引用主机 policy 中的既有 certificate/key 路径和准确域名列表；网页只选择引用，不上传私钥。证书内容纳入计划指纹，`nginx -t` 检查文件及证书/密钥。默认只提供 HTTP，不会申请证书或修改 DNS。HTTPS 的真实握手、域名和到期状态仍需单独验收。

应用前固定当前项目/策略/证书指纹，先 [nginx -t](https://nginx.org/en/docs/switches.html)，成功后才切换配置。应用后通过专用状态路径核对实际加载的 plan marker；命令退出 0 不直接当作入口完成。Linux 上用 pid、进程启动时间、实际可执行文件及 prefix/config 身份验证归属，不向未核对进程发送 reload/quit。

使用 `--operations` 的原生 Admin 重启时，只恢复 SQLite 中上次已确认、且与当前登记及实际配置完全一致的 plan。存在未应用修改、配置漂移、旧 pid 对应进程仍存在或身份不明时停止恢复，Admin 保持可用供人工处理；不会自动发布待应用配置。

检查失败保留原配置；切换失败尝试恢复原文件并重载原 plan，首次启动失败仅退出已确认归属的新 master。失败与回退是否核对均持久记录。超时、身份不明或回退未确认时停止自动动作，不能靠重试或删除 pid 文件绕过。

## 常驻服务授权

项目自行维护已有 systemd system/user service。主机 policy 登记准确 unit 和 scope，默认只授予 status/logs。例如：

```json
{"scope":"user","unit":"example.service","actions":["status","logs"],"definition_sha256":null}
```

先用 `scripts/service-hub.py service-status --state ... --alias example` 读取 `definition_sha256`。指纹覆盖主 unit 和 drop-in 文件内容；原文和 Environment 不回传。需要 start/stop/restart 时，由管理员在新的 policy revision 显式加入操作并固定这个指纹。NeedDaemonReload 不明确为 no、定义变化、项目/策略变化或最新运行观察不匹配时拒绝写操作。

网页再次输入完整项目名称确认，执行固定参数数组，通过 systemctl 的非交互权限执行；没有任意命令、kill PID、自动 sudo、daemon-reload、enable 或创建 unit。管理服务自身不可通过这个入口启停。日志最多 100 行/64 KiB，常见凭据脱敏；不能把日志脱敏等同于内容完全无秘密。

操作意图先持久保存，再执行，最后回读实际 active/inactive 状态。同一请求 nonce 返回原回执而不重放；同键不同输入拒绝。超时/重启保存为未确认，不自动重放；系统状态通过仍需业务健康检查。最多保留 200 个操作，到限停止新运行操作，需审查归档策略，不能无限增长或静默淘汰未确认记录。

## XDocker 自身常驻

`scripts/admin-native-unit.py` 只渲染 unit，不安装、不启用。部署方案审核时指定准确 checkout、venv Python、私有 state、用户/组和端口。例如：

```sh
python3 scripts/admin-native-unit.py --checkout /data/workspace/XDocker --state /data/workspace/XDocker/data/native-admin --python /data/workspace/XDocker/.venv/bin/python --user michael --group michael --port 19081
```

取得明确部署授权后再安装到 systemd，先 verify，再启用这一个服务；初次默认没有 `--operations`。修改业务项目或它们的 unit，由对应项目维护者处理。只读盘点、原生启动代码及 fixture 通过都不代表已经在 home-pc-ubuntu 安装。

## home-pc-ubuntu 管理访问

已部署实例位于 `/data/workspace/XDocker`，systemd 服务为 `xdocker-native.service`，Admin 保持 loopback 19081。连接 VPN 后，在自己的终端开启转发，再打开 `http://127.0.0.1:19081/#service-hub`：

```sh
ssh -N -L 19081:127.0.0.1:19081 home-pc-ubuntu-vpn
```

另一个终端读取自己的登录令牌，不要将结果发到聊天或提交 Git：

```sh
ssh home-pc-ubuntu-vpn 'cd /data/workspace/XDocker && .venv/bin/python scripts/admin-native.py token --state data/native-admin'
```

本次网关使用 `scoreboard.home.test:8088`、`docs.home.test:8088`。访问设备须将两个域名解析到 home-pc-ubuntu 的 VPN 地址；可使用受管 DNS 或自己设备的 hosts。服务端 Host 转发检查及客户端 DNS 是两个步骤；本次未改客户端系统 hosts/DNS。部署状态与实机证据见 [验收记录](../../tasks/native-services-acceptance-20261112.md)。
