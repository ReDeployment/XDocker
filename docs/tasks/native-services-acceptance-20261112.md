# 独立服务与原生管理入口验收

日期：2026-11-12。状态：本地实现已推送到独立任务分支，home-pc-ubuntu 原生实例、SQLite 与两个真实网页入口已部署并通过实机验收；当前 Mac 的域名解析和 HTTP 80 标准入口已修复，其他设备仍需各自解析或共享内网 DNS。

## 定位与范围

项目自行开发、构建和启动，XDocker 管入口、资源关联和受控运维。业务应用不需要为接入 XDocker 增加 Dockerfile。当前轮实现独立项目登记、原生 Admin、固定健康探测、Nginx 网关预览/检查/应用、已有 systemd 服务状态/日志及明确授权的 start/stop/restart。交付方式仅登记交接，不提供任意构建/安装/迁移命令，不把保存字段称为发布成功。

工作树 `/Users/michaelhu/.codex/worktrees/xdocker-host-resources-20261112`，任务分支 `codex/host-resources-20261112`，基线为最新 fetch 后的 `origin/main` `13653a6c8dab6867a9745301b94242dad677415f`（开始时相对远端 0/0）。功能实现已提交推送，最新运行逻辑变更为 `d9c0b8435915e7a21787991d4cb6c2008d75ffdd`；后续文档/UI 微调同样按任务分支同步。该分支承接上一轮资源监控，仅包含这两项功能；不混入原工作区未提交的交付中心、AI、升级等切片。

原工作区也完成兼容接入，保留原有模块。开始前保存 332 个公共文件内容摘要；既有文件增量限于列明的 14 个集成/测试/文档文件，无删除、无名单外改动。没有修改 PadelU、PadelScoreboard、Deploy、PowerXDocs、Core、Framework、Market 或其工作树/数据库。

## 主要文件与边界

- `scripts/admin-native.py`、`services/admin/native_runtime.py`：原生 init/serve/token，独立 0700/0600 状态与 checkout 身份，Gunicorn 重载，可选完全禁用 Docker；Linux 指标采集在 reload worker 外运行。开发 reload 与运行操作不可并用，旧的容器恢复及写操作不开放。
- `scripts/admin-native-unit.py`：只渲染 XDocker 自身 unit，不安装、不启用，默认不开放运维操作。
- `native_services.py`、`service_hub_api.py`：主机私有 endpoint/unit grants，项目 revision、域名/服务归属冲突、资源绑定、固定健康路径、状态/脱敏日志和持久操作回执。固定 systemctl 参数，不使用 shell、sudo、kill PID、enable 或 daemon-reload。unit/drop-in 内容指纹与最新观察必须匹配。
- `gateway_runtime.py`：独立 Nginx prefix/config/pid，原生端口与 WebSocket 转发，既有 TLS 引用，计划漂移检查、nginx -t、marker 回读及失败恢复。Linux pid/启动时间/exe/prefix/config 身份不明时不接管。已有全局 Nginx 不受接管。
- `static/service_hub.js/css`：接入概览、项目登记、网页入口、常驻服务、操作记录，桌面纵向/窄屏横向分区。草稿保留、revision 冲突保留输入，窄屏弹窗保存按钮持续可见。运行结果/回退异常在分区外显示。
- Admin 打包/初始化、现有 app 接入、主机 collector 可选 Docker、专项测试/CI、原生指南与 JSON 示例。原工作区另更新已有 bootstrap 和只读 admin-view；镜像页只读，不代理运行 POST。

地址/服务授权只能由主机私有 CLI 配置；HTTP 只选择既有别名。保存项目不探测或启动应用，网关默认禁用，服务默认只读。写操作还需 native `--operations`、准确 unit grant、固定定义、最新观察及完整名称确认。操作意图先落盘，按请求 nonce 幂等；同请求返回原回执，不重复重启，超时/进程中断保持未确认。网关相同 plan 且实际 marker 匹配时复用回执，不重复 reload。

## 验证

- 原工作区相关回归最终 108 项通过：native services/gateway、原 Admin auth/CSRF/审计、host metrics/resources、delivery control、只读视图与资产读取。旧 job 测试的 wait helper 改为等待终态审计后才结束，避免测试清理目录与后台审计竞态；运行任务代码未为此改变。
- 独立部署分支另行复验 native/services/gateway/Admin/metrics/resources；52 项通过；服务器使用实际 pinned 依赖及独立 Nginx 包复验同样 52 项通过，无跳过。不能用原工作区结果代替分支结果。
- 真实独立 Gunicorn 进程经 native CLI 初始化和启动，在明确禁用 Docker 的情况下完成 HTTP readiness、登录、CSRF、原生 snapshot、页面脚本与独立服务读取，确认运行操作默认关闭，登出后关闭进程并清理自己的临时状态。
- 使用本机已有 Nginx 二进制，在全新临时 prefix 和随机 loopback 端口执行真实配置检查、HTTP 上游转发、101 WebSocket Upgrade，以及状态 plan marker；结束后 quit 自己的 master 并确认 pid 文件消失。未管理系统 Nginx，未安装系统包。该测试不是 Linux 进程归属或 systemd 实机写操作验收。
- 真实 Chrome + 临时 Flask 验收 1440×1000 和 390×844：五分区、默认概览、登记不启动、草稿/刷新保留、资源关联、健康、配置预览/应用和一次已授权服务操作（仅 fake adapters）、日志脱敏、异常持续可见、窄屏活动导航及可见保存按钮。0 JS 异常；fixture 确认没有 Docker 写操作，服务 restart 只触发一次替身。原有交付中心七分区验收通过。
- 公共打包目录静态导入、CI YAML/示例 JSON、JS 语法与 diff 空白检查通过。没有可用本机 Docker daemon，未执行真实 Admin 镜像构建；原生入口的实际 HTTP 验收已单独完成。

浏览器临时服务和 profile、原生 smoke 与真实临时 Nginx 均已清理。截图仅包含 fixture 数据，在本机 `/tmp/xdocker-service-hub-primary-ui-20261112/` 与 `...-branch-ui-20261112/`。

## home-pc-ubuntu 只读快照与项目交接

用户给出的 `/data/workpsace/` 不存在，实际为 `/data/workspace/`。以下是本轮读到的目录、监听和进程 CWD，迁移中的状态可能继续变化：

| 项目/入口 | 读到的运行证据 | 接入边界 |
| --- | --- | --- |
| PadelScoreboard | `/data/workspace/PadelU/PadelScoreboard`；VPN 18080 的两个 Python PID，CWD 匹配项目 | 独立原生运行；尚未核对现有 unit，不替项目生成/安装 unit。HEAD 根路径为 405，后续 GET 连接在 SSH 映射变化时中断，不能记为健康通过 |
| PowerXDocs 当前站点 | `/data/workspace/PowerX/Core/PowerXDocs`；powerxdocs-local 容器映射 VPN/LAN 8080，HEAD `/` 200 | 保留项目现有容器启动方式，只接入口和监控 |
| PowerXDocs 合并预览 | powerxdocs-merged-preview 映射 VPN/LAN 8081，HEAD `/` 200 | 单独登记预览，不根据名字猜源码目录 |
| PowerXDocs 开发工作树 | VPN 5173 的 Node PID，CWD `/data/workspace/PowerX/Core/PowerXDocs-production-fix-20261112`；HEAD `/` 200 | 原生开发服务；不是 PadelU。新增域名的框架 Host/HMR 策略由项目维护者核对 |
| PadelU | `/data/workspace/PadelU/PadelU`；开发文档提到 5001/5173/5432 | 文档端口不等于运行；未确认 API 5001 正在监听，待项目维护者完成迁移与启动 |
| Deploy | `/data/workspace/PadelU/Deploy`；文档提到 5100/9443 | 已有独立部署/备份控制项目，未接管其生命周期/数据库；本轮对应端口未在监听清单出现 |

PadelU 下的 autobackup 工作树不登记为独立应用。PowerXDocs/repos 下的 powerx、powerx-plugin、powerx-marketplace 是文档项目内的采集副本，不能据此推定 Core/Framework/Market 已独立迁移运行。匹配的系统级 service 查询为空，不据此推定所有 user manager 都没有 service。

主机当时没有 nginx 可执行文件。已准备 4 个私有 endpoint/project 草案，并在本机私有临时 state 做 schema/revision/冲突校验。实际地址仅保存在私有草案，不硬编码到公共示例。所有草案 enabled=false、service/monitor=null，policy gateway.enabled=false；未导入服务器、未配 DNS/TLS、未授予服务启停、未把模板标为 Ready。

## 部署授权与连接历史

只读盘点期间 SSH 别名发生变化：早先 `home-pc-ubuntu` 映射真实 VPN 地址、michael，后续 `ssh -G` 输出变为 `workspacex-no-route.invalid`、michaelhu，DNS 解析失败。后续核对为 WorkspaceX 管理的路由回退；VPN 也曾实际超时。没有修改 SSH 配置，用户确认恢复后通过已有 VPN 别名继续。

此前部署建议已由后续授权更新：用户要求开始执行，并指定 SQLite。实际目标为 `/data/workspace/XDocker`，原生 Python/venv + XDocker 自己的 systemd unit；网关使用独立 Nginx prefix 和高位端口。服务器必须同步已推送的准确功能 SHA，不能复制未提交源文件绕过本地流程。

## SQLite 增量验收

项目、可信地址/服务/证书引用、网关设置/回执、监控分组/绑定和原生操作回执已迁入 `registry.sqlite3` 的关系表。5 项新增 SQLite 测试覆盖源文件保留及 SHA 备份、一致性备份 API、重新打开持久化、迁移后旧写入阻断、重复域名导致整笔 DDL/数据回滚、唯一组件约束和独立并发写入、未知 schema/symlink 拒绝。

原生网关增加重启恢复：只恢复已经应用的准确 plan；新配置等待人工应用，仍存在的旧 pid 不可删除或被接管。新增测试覆盖幂等恢复、进程退出后恢复、待应用修改拒绝以及仍在运行的 pid 阻断。

2026-11-12 部署前只读复核：SSH 身份为 michael@home-pc-ubuntu，目标目录尚未存在；19081/8088/8443 空闲；VPN 100.100.0.3 上 18080/8080/8081/5173 的 GET `/` 均返回 200。Nginx 尚未安装；GitHub SSH 读取缺少主机信任，正在核对 HTTPS 拉取方式。此记录不把本地通过当作远程部署完成。

## 实机部署验收

- 目标 `/data/workspace/XDocker` 从 GitHub 任务分支拉取，逐次核对推送后的准确 SHA，远程工作树干净。Python 3.12.3 venv 安装 requirements 的实际 pinned 依赖；原生 Gunicorn HTTP smoke 在服务器通过。GitHub/PyPI 访问使用本次 SSH 临时转发，未改系统 Git/proxy 配置。
- `xdocker-native.service` 经 systemd-analyze verify 后安装并启用，状态 enabled/active。Admin 只监听 loopback 19081；指标在 worker 外每 15 秒采集，实际发现宿主机进程与 Docker 组件。Gunicorn 控制 socket 关闭，避免复用用户目录中的共享控制入口。
- 从 Ubuntu APT 下载 nginx 1.24.0-2ubuntu7.19，在 XDocker 的 data/runtime/nginx 解包使用；包 SHA-256 为 `b9c3476a504c963d8c3cab6540127fa4100ccf6ddf8ee978780bb32f128968d5`。没有安装/启动全局 nginx，没有改 /etc/nginx。所有 Nginx 临时目录均指向自己的 prefix。
- SQLite 保存两个项目、两个固定接入地址、两个监控分组/绑定、一条已加载网关回执；服务写授权及业务运行操作记录为零。PadelScoreboard 绑定实际 `padelscoreboard-dev.service` 的 4 个进程，PowerXDocs 绑定正在运行的 powerxdocs-local 容器，旧已退出的记分板容器未被选入。
- 两个原始 GET `/` 200，网页入口 `scoreboard.home.test:8088`、`docs.home.test:8088` 用真实 Host 转发均为 200，plan marker 与 SQLite 指纹一致。Mac 经 VPN 使用 curl --resolve 复验均为 200。客户端系统 DNS/hosts 未修改，直接域名解析尚未完成；TLS 与开发 HMR 也未做真实业务验收。
- 停止已核对归属的 XDocker 自己的 Nginx，再立即重启 XDocker 自身：原有 plan 自动恢复，新 Nginx pid 与数据库一致，两个入口继续 200，记分板原 4 个 pid 都保持存活。整机重启未执行，不能用此替代完整主机启动验收。
- `PRAGMA integrity_check` 返回 ok，registry.sqlite3 为 0600；一致性 backup API 生成 data/native-admin/backups/registry-post-deploy-20261112.sqlite3，SHA-256 `b00f9a77ddc602d007be5f74934768944caef3d8c2a4c859f698c9f7016ad8fe`。
- 实际远端管理页经独立临时 SSH 转发及 Chrome profile 验收，1440×1000 与 390×844 的五分区、两条真实登记、监控关联、健康和实际网关版本均通过，0 JS 异常；截图位于本地 /tmp/xdocker-service-hub-live-20261112/。实机没有点击业务启停或生成测试项目。

部署中发现并从本地修正：systemd WorkingDirectory 标量引号、Ubuntu Nginx 默认系统临时目录、nginx -t 留下的空 pid 文件、TIME_WAIT 导致的快速重启端口误判。增加针对性门禁/测试；非空未知 pid、活跃监听和配置漂移仍拒绝，进程启动只重试读取身份而不重放命令。所有修复先本地测试、提交推送，再远端拉取运行。

失败回退只停止本次新增的 XDocker 自身与自己的网关，保留状态/回执，恢复相应配置；不删除业务容器、卷、数据库，不对其他项目执行 reset、迁移或重启。

## 标准入口修复验收

用户反馈 `.home.test:8088` 不能访问并要求统一入口省略端口，随后明确授权 fix。确认当前 Mac 没有两个域名的解析，服务器仅监听 VPN 8088；指定 Host 能返回 200，80/443 空闲。

- 实现提交 `abf552c5286f90953a5c17eda3c89441759d08b3`。策略支持 HTTP 80 / HTTPS 443 或既有高位开发端口，拒绝其他低位端口；页面链接与 HTTPS 跳转省略默认端口。权限不足单独返回 XD_GATEWAY_BIND_PERMISSION_REQUIRED。
- unit 使用显式 `--operations --gateway-web-ports` 渲染，仅允许 CAP_NET_BIND_SERVICE，User 仍为 michael，NoNewPrivileges=yes。服务器核对 AmbientCapabilities/CapabilityBoundingSet 均只有这一项，没有改全局低端口 sysctl、没有以 root 运行 Admin、没有改业务 unit。
- 切换前保存 SQLite 一致性备份、旧私有 policy 及原 XDocker unit。先安装并 verify 新 unit，复核旧 8088 网关身份正常，再将 SQLite policy revision 从 2 更新到 3，HTTP 80 / HTTPS 443，按准确指纹应用。实际仅监听 HTTP 80；443 是待配置的 HTTPS 端口，未配置证书，不对外声称 TLS 可用；8088 已停止监听。
- 当前 Mac /private/etc/hosts 在保留原有条目、owner/mode 的情况下追加两个精确名称到服务器 VPN 地址；修改前有私有原文件备份和 SHA 回执。没有修改系统代理、VPN/DNS 服务或其他项目文件。其他设备的解析不因当前 Mac hosts 自动生效。
- 从 Mac 直接 curl 不带端口的两个域名均为 200，无 --resolve；真实 Chrome 使用系统实际 DNS/代理，无 host-resolver-rules，访问 http://scoreboard.home.test/ 返回 200，标题 PadelU · 比赛工作台；http://docs.home.test/ 正常进入 /zh/，标题 PowerX 智能体平台 | PowerX Documentation，响应 plan marker 与 SQLite 一致。
- 只重启 XDocker 自身后，HTTP 80 网关自动恢复且指纹一致，两个入口继续 200；记分板原 4 个进程保持存活。SQLite integrity_check=ok，修复后 backup API 一致性备份 SHA-256 `abd1dae1ad5b6f0583076858c5731e37466c3d6f6dba7901279b2e6115ca05e0`。
- 本地主工作区 109 项、独立任务分支 53 项通过；服务器用 pinned 依赖和独立 Nginx 运行同样 53 项通过。Chrome 分区验收覆盖 1440×1000 / 390×844，以及 80/443 隐藏端口、8088/8443 保留端口的链接行为。

用户要求的 worktree 合并后收尾规则已加入 AGENT.md；本任务分支尚未合入目标分支，因此保留活动 worktree，不把部署完成当作已合并，也不强删含未提交工作的旧目录。
