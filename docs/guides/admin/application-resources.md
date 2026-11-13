# 应用资源监控

本地实现日期：2026-11-12。入口为「系统管理 → 应用资源」（`/#resources`）。本次只提供监控、应用分组登记和历史读取，不增加应用启停、Git 提交、代码同步或自动发布权限。

## 工作方式

项目源码可以继续留在开发电脑。XDocker 源码也在本地开发、验证和推送；服务器只同步已推送的固定提交，再运行自己的 XDocker 与采集器。采集器观察服务器上的运行负载，不要求把开发仓库搬上服务器；若服务器需要从源码构建某应用，那是该应用另行登记的构建需求。

控制台依次分为服务器概览、GPU 资源、已登记应用、发现应用、历史趋势。桌面左侧纵向任务导航；窄屏横向导航，当前只展示一个分区。表单在弹窗中编辑，取消、分区切换和定时刷新保留未保存草稿。采集错误、过期、容量限制及资源提示在所有分区外可见。

宿主机 `scripts/host-monitor.py` 是无第三方 Python 依赖的有界单次采集器；读取 `/proc` 和固定 [Docker Engine API](https://docs.docker.com/reference/api/engine/version/v1.44/) GET 接口，将私有 JSON 与 SQLite 历史写到 `data/host-metrics/`。Admin 只读挂载此目录，不挂载宿主机 `/proc`，不使用 host PID namespace。默认以部署用户运行；无权读取、已退出及内核进程会计入覆盖说明，不冒充完整应用覆盖。

- 整机：CPU、逻辑 CPU 数、可用/总内存、Swap、负载及根分区可用空间。
- 宿主机应用：优先按 UID 与 systemd service 分组；其他按 UID、可执行文件与工作目录分组。同目录下多个可执行程序可以作为多个组件登记到一个应用。进程详情有 PID、父 PID、名称、CPU 与 RSS，不采集命令行参数、环境变量或应用文件内容。
- 容器：发现当前 Docker daemon 的容器；Compose 项目/服务标签作为稳定身份，独立容器使用完整容器 ID。Compose 同名副本不能合并，退为各自的容器身份。独立容器重建后需重新绑定。容器进程从宿主机应用中排除，避免重复统计。
- 登记：每个应用最多 16 个组件，同一组件只能属于一个应用，最多 100 个应用。共享数据库建议单独登记。宿主机内存为 RSS 合计，共享页可能重复；容器为扣除 inactive_file 的工作集，两种口径不能简单当成整机已用内存。
- CPU：应用以单核为 100%，整机以全部 CPU 为 100%。首次采样、进程新启动或计数器重置时 CPU 保持待采样。进程以 PID 加启动时间计算差值，避免 PID 重用产生错误峰值。
- 历史：采集器每分钟保存一次，滚动保留 24 小时；页面每五分钟显示 CPU 均值与内存峰值，缺失采样保留空白。多组件应用只汇总组件都有样本的分钟，不将缺失组件算成 0。趋势按当前登记组件回看，不是历史配置审计。

超过 90 秒的报告显示过期，应用汇总保持未确认。进程上限 4096、宿主机分组上限 128、容器上限 32；触限和部分容器失败持续显示。容器最多 4 个并行读取，单请求 1 秒，采集进程由 systemd 的 30 秒上限保护。报告限 4 MiB，历史数据库限 64 MiB；到限报告历史不可用，不自动扩大容量。历史没有完整数据时不能根据空图判断资源空闲。

当前资源提示针对本次采样：整机 CPU ≥85%、可用内存或根分区空间 <10%。它们不是持续高负载告警，没有外部通知或自动重启。

## 准备及运行

以下是有待明确授权的服务器安装步骤，文档与本地测试不代表已执行。先按 [AGENT.md](../../../AGENT.md) 完成本地验证、独立任务分支与审查流程，推送后再让服务器同步准确 SHA。其他项目和既有容器不随本步骤升级。

1. 在服务器自己的 XDocker checkout 中完成既有 [Admin 初始化](README.md)。`admin.sh init` 同时建立 `data/host-metrics/`，0700，所有者与 Admin 运行 UID/GID 一致。已有安装升级也须运行 init 并备份私有 Admin state；不要输出或提交令牌。
2. 确认部署用户能访问 `/var/run/docker.sock`，只读检查后运行一次 `python3 scripts/host-monitor.py --output /实际路径/XDocker/data/host-metrics`。脚本仅写自己的采样目录；不能在 macOS 上采集 Ubuntu 指标，也不能用一次成功代替定时器验收。
3. 在本地渲染并审查两份 systemd unit。以下路径与用户须换成真实值：

```sh
python3 scripts/host-monitor-unit.py service --root /home/michael/xdocker --user michael --group michael > /tmp/xdocker-host-monitor.service
python3 scripts/host-monitor-unit.py timer --root /home/michael/xdocker --user michael --group michael > /tmp/xdocker-host-monitor.timer
```

4. 取得部署授权后，将已审查的 unit 安装到服务器 `/etc/systemd/system/`，`systemd-analyze verify` 通过后 daemon-reload，启用这一计时器。unit 以部署用户采集，限制可写目录、地址族和运行时长；显式设置 [TimeoutStartSec](https://www.freedesktop.org/software/systemd/man/257/systemd.service.html) 为 30 秒。它不会赋予原本没有的 Docker/procfs 权限。Admin 新镜像及 Compose 只更新 Admin，不启停其他应用。
5. 私有入口继续绑定 `127.0.0.1:9080`，通过现有 VPN 加 SSH 转发访问，不新增公网管理端口。在控制台核对服务器名、报告时间、采集覆盖，再从发现应用登记组件。登记不会把新容器加入原来的启停白名单。

采集器写报告需与 Admin 使用相同的私有目录所有权。报告和 registry 都是 0600；身份和目录权限异常时失败，不通过放宽为全局可读解决。备份 Admin state 中的 `resources/` 及需要保留的采集历史。回退时停用这一计时器、恢复对应 Admin 镜像/配置和匹配状态快照；采样文件可单独保留，不删除业务数据或停止被观察应用。

## 本地验收与当前部署边界

```sh
PYTHONPATH=tests:services/admin:scripts python3 -B -m unittest test_gpu_metrics test_host_metrics test_resource_monitor test_admin
python3 -B tests/admin_build_context_smoke.py --static-only
node tests/resource_browser_acceptance.cjs
```

浏览器使用隔离的临时 Flask、假 procfs/容器报告及独立 Chrome profile，检查桌面与窄屏任务导航、登记与编辑、草稿保留、过期与错误提示、历史和监控不授权运行操作。Linux 实机、定时器自启、实际 Docker 计量与服务器访问须另行记录，fixture 不能作为部署验收。

## GPU 资源（2026-11-13）

概览显示 GPU 利用率摘要；“GPU 资源”分区显示当前设备、利用率、温度、显存及共享内存 GTT。多设备选择在切换分区和刷新时保留。CPU、内存等已有指标与应用绑定不受 GPU 读取失败影响。

当前支持 Linux `amdgpu` 的只读 sysfs 采样，不执行 GPU 命令、不打开 `/dev/dri`、不更改权限或驱动。发现 `/sys/class/drm/cardN/device` 后按物理设备去重，以 PCI 地址生成稳定身份；最多 8 台设备、256 个发现条目、16 个 hwmon 条目，单属性最多 4 KiB。缺失、无权读取、越界或不支持的指标为 null，页面显示“不可用”；旧报告显示“尚未采集”，不会将缺失值补成 0。

- `gpu_busy_percent` 是设备忙碌比例；有效的 0% 表示这次采样空闲。温度由 `temp1_input` 的毫摄氏度换算。接口定义见 [Linux AMDGPU 文档](https://docs.kernel.org/gpu/amdgpu/thermal.html)。
- home PC 的 `1002:1638` Cezanne 核显将 VRAM 标为“预留显存”；GTT 用量和驱动上限单独显示。GTT 上限来自 `mem_info_gtt_total`，属于系统内存的可用上限，不是额外物理容量，不能与总内存或预留显存相加。
- APU 的 `power1_input` 包含 CPU，当前不展示为 GPU 功耗。不提供应用/进程/容器 GPU 归属，也不将设备当前采样写入已有 CPU/RSS 历史表。其他驱动仅展示身份与“不支持”状态。

报告保持 schema 1，新增可选 `gpu` 字段；API 对字段、范围和设备数独立校验，拒绝透传 sysfs 原始内容。无数据库迁移。回退到旧提交只会恢复原有 CPU/内存页面；GPU 字段可随下一轮采样覆盖。
