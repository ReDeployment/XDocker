# 应用资源监控验收记录

日期：2026-11-12。状态：本地实现、隔离测试与真实浏览器 fixture 通过；没有提交、推送、构建镜像或部署服务器。

## 源码范围

独立工作树：`/Users/michaelhu/.codex/worktrees/xdocker-host-resources-20261112`。
独立任务分支：`codex/host-resources-20261112`，基于已 fetch 的 `origin/main`：`13653a6c8dab6867a9745301b94242dad677415f`。本文件不是本功能的发布 SHA，功能尚未提交。

原工作区 `/private/var/www/html/ArtisanCloud/X/XDocker` 已有大量未提交修改，本地 main 相对远端落后 14 个提交，未切换或强制覆盖。采集器、登记模块、前端、脚本与测试在原工作区也完成了兼容接入；独立任务工作树仅含监控功能，不携带交付中心、升级准备、AI 协作等未提交切片。启动前记录的 235 个已有修改/未跟踪文件，经内容摘要检查，未删除文件、未改动本次名单之外的文件。

本功能文件：

- `services/admin/host_metrics.py`：Linux 与固定 Docker GET 采集，CPU 差值、PID 重用检查、覆盖限制、24 小时历史和私有原子报告。
- `services/admin/resource_monitor.py`：应用/组件绑定、revision、互斥归属、服务器身份核对、过期与不完整状态、只读历史 API。复用会话、Origin、CSRF 和审计，不扩展容器启停白名单。
- `services/admin/static/resources.js`、`resources.css`，以及 app/index 入口：概览、登记、发现、历史四分区，详情与编辑弹窗，草稿保留，错误在分区之外可见。
- `scripts/host-monitor.py`、`host-monitor-unit.py`：有界单次采集与纯渲染 unit；渲染不安装或启动任务。
- Admin Dockerfile/.dockerignore/Compose、admin.sh：打包公共运行代码，只读挂载新指标目录，由 init 创建匹配 UID/GID 的私有目录。
- 专项 tests、私有配置排除/静态导入检查、监控 CI、使用文档。原工作区另外适配已有 bootstrap 和只读 admin-view；它们不是该远端基线的部署依赖。

## 已执行验证

- 独立分支：`PYTHONPATH=tests:services/admin:scripts /opt/anaconda3/bin/python3 -B -m unittest test_host_metrics test_resource_monitor test_admin`，最终 27 项通过（只复用旧 fixture helper，避免重复发现旧测试）。覆盖计数器/重启/PID 重用、systemd 与容器去重、首次 CPU 待采样、读权限与容量、缺失组件、登记持久化/revision、跨应用重复绑定、历史缺口、认证/CSRF 及不增加运行权限。
- 较早重复发现旧 job 测试的运行曾出现临时目录清理后后台审计线程才结束的输出；未修改运行任务逻辑。专项 API 测试改为只复用旧 fixture helper，最终 27 项运行干净通过。
- 独立分支静态公共打包目录导入通过；Compose 和 CI YAML 解析、JS 语法及 diff 空白检查通过。
- 独立 Chrome + 临时 Flask + 假 procfs/容器报告，在 1440×1000 与 390×844 验收四分区、默认概览、实际登记、编辑草稿、历史、持续可见的过期/错误以及窄屏横向导航。无 JS 例外；fixture 退出确认 Docker 调用全部为 GET，移除临时服务和 Chrome profile。
- 原工作区相关回归 108 项通过、5 项因不启用 Docker 验证而跳过；原有交付中心七分区浏览器验收通过。新监控浏览器也在原工作区通过。
- 独立浏览器截图位于本机 `/tmp/xdocker-resource-branch-ui-20261112/`；已实际检查桌面概览/历史、窄屏应用/编辑窗口。图中 fixture 数据不是服务器实测结果。

## home-pc-ubuntu 只读核对

SSH 别名解析到现有 VPN 地址；实际主机名 `home-pc-ubuntu`，登录用户 michael，UID/GID 1000/1000。16 个逻辑 CPU，总内存 65,229,168 KiB（约 64GB 级别）；该次检查可用内存约 62,114,252 KiB。home 分区可用空间约 265.2 GiB。Python 3.12.3、Git、Docker 29.9.3 可用；登录用户可读取 Docker 容器列表。9080 和 19080 在核对时未监听。

该次列表有 powerxdocs-local、padel-scoreboard-home-web-1 及四个 Immich 容器。未启停它们、修改它们的配置或读取凭据。常见 XDocker 目录未找到 checkout，不代表已经穷举整块磁盘。

## 未验证及拟部署范围

本机 OrbStack Docker socket 的 daemon 不可达，未启动它，未完成真实镜像构建/过滤验收。Linux 实机采集、systemd unit verify/自启、真实指标与系统工具对照、镜像运行及访问登录尚未验证；源码和 fixture 通过不代表服务器已部署。

待明确授权后：仅将本功能提交并推送至 `origin/codex/host-resources-20261112`，在 `home-pc-ubuntu:/home/michael/xdocker` 同步准确功能 SHA；构建并固定本机 Admin 镜像，初始化独立的 XDocker 私有状态；只启动 Admin 和 `xdocker-host-monitor.timer`，管理入口绑定 `127.0.0.1:9080`，通过 VPN 内 SSH 转发访问。不新增业务实例，不把既有容器加入启停范围，不改其他项目、不推送共享分支、不自动执行 Git 同步或应用重启。

首次安装前核对目标目录为空/属于本实例、端口仍空闲及原有 unit 不存在；如已存在则保留并核对，不能覆盖。保存本次配置与 unit 备份、固定 SHA/镜像身份；验证单次采集、连续报告新鲜度、历史写入、页面/API、重登与进程归属，并回读既有服务状态。失败时停止且仅移除本次新增的 Admin/采集 timer，保留日志/状态，不触及业务容器、数据卷或数据库。
