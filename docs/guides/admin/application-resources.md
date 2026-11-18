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

## 硬盘与存储（2026-11-13）

入口为 `/#resources/storage`，根分区卡片也可直接进入。默认只展示物理硬盘列表；分区、LVM 与其他逻辑设备折叠查看。设备详情弹窗分为“容量与挂载”和“读写与健康”，一次显示一个任务分区，刷新和关闭后重新打开同一设备保留所选分区。窄屏导航可横向滚动，刷新后仍确保当前导航可见。

采集器以当前用户读取 sysfs 块设备属性、`stat` 计数和 `/proc/self/mountinfo`；仅对发现的本地块设备挂载点读取文件系统统计，不扫描目录或读取业务文件。跳过 loop、RAM、空设备及伪文件系统；最多发现 64 个块设备、512 个 sysfs 条目、128 个文件系统（每个最多 16 个挂载位置），挂载输入限 1 MiB / 4096 行。设备消失、无权访问和报告不完整均明确保留未知状态。

- 型号、接口、SSD/HDD、物理容量与分区/LVM 上下级分开显示。LVM 可经底层分区关联物理盘；同一文件系统的多个绑定挂载合并，物理盘、分区与逻辑卷容量不相加。使用 GiB / TiB 二进制单位，与厂商 TB 标称不同。
- 文件系统展示总量、已用、普通用户可用、保留空间和只读挂载。使用率为 `used / (used + available)`，与 `df` 一致；已用不包含保留空间。卸载分区不推测文件系统容量，可用空间 <10% 的挂载位置在所有资源分区提示。
- 读写吞吐、读/写 IOPS 和 I/O 忙碌时间按相邻样本差值计算。扇区计数乘 512 转换为字节，见 [Linux I/O 统计文档](https://docs.kernel.org/admin-guide/iostats.html)。首次、超过 90 秒的采样间隔、重启、`diskseq`/设备实例改变或计数回退时，速率保持待采样。忙碌时间比例不是带宽饱和度，不能据此单独判定 NVMe 性能瓶颈。
- 温度优先读取设备自己的 hwmon。已有 `smartctl` 且当前用户有块设备读取权限时，只对已知直连 NVMe/ATA 类型读取 `-j -d <nvme|ata> -n standby,3,5 -H -A` 的既有 SMART 状态；USB/未知桥接不自动探测类型；每轮最多 4 次、每次限 1 秒，60 秒内复用当前设备实例的结果。SMART 原始输出不写报告或返回 API，仅保留温度、通过/告警/不可用和检查时间。不执行自检、不唤醒待机盘、不安装工具、不运行 sudo、不修改设备权限。无权读取时显示不可用，不推测健康。

GPU、CPU 与原有内存历史不依赖磁盘采集成功；可选 `storage` 字段单独校验并使用字段白名单。没有 SQLite schema 迁移，也不增加分区、格式化、挂载、扩容或文件删除操作。设备当前 I/O 不写入原有 CPU/RSS 历史表。部署只更新并重启 XDocker，先备份自身 registry/同步配置和部署回执；回退到部署前固定提交后重启 XDocker 即可恢复旧页面。

验收：`test_storage_metrics` 覆盖拓扑、LVM、绑定挂载去重、保留空间、扇区换算、重连/重启/计数重置、权限与 SMART 故障隔离、API 字段白名单和旧报告。`resource_browser_acceptance.cjs` 使用隔离 fixture 验证 6 个资源分区及硬盘详情的桌面/窄屏、选项保留、错误可见和无运行控制。真实部署需另行对照实机块设备与 `df`，并等待两轮采样核对 I/O；fixture 不代替真实设备验收。

## 挂载配置与按需挂载（2026-11-15）

物理盘列表和“容量与挂载”详情同时展示实际挂载点及 `/etc/fstab` 配置的本地块设备目录。通过 `/dev` 下的设备节点、UUID/PARTUUID/LABEL/PARTLABEL 链接关联已发现的设备，不依赖易变的 `sda` / `sdb` 名称。只读取节点元数据，不打开块设备。

- “已挂载”：本次 `/proc/self/mountinfo` 已发现该设备和目录，可读取容量；按需或开机挂载方式另行显示。
- “按需挂载，等待访问”：fstab 含 `x-systemd.automount`，对应目录存在实际 autofs 入口，但尚未发现块设备文件系统。配置语义见 [systemd.mount](https://www.freedesktop.org/software/systemd/man/latest/systemd.mount.html)。此状态不代表已经验证挂载成功，也不包含历史失败记录。
- “已配置…，未挂载”：有配置目录，但没有匹配的实际挂载；不会把配置当成已挂载或伪造可用容量。
- “未配置挂载”：只有配置读取完整且没有关联目录时才显示。读取失败、标识解析不完整、旧采集器或发现达到上限时保留“未发现已挂载文件系统”，同时提示配置尚未确认。

采集器对配置输入限 64 KiB / 512 行，最多 128 条关联记录。报告仅新增 `mount_plans` 和 `mount_configuration_available`，不公开原始配置、设备 UUID、挂载参数或其中可能含有的凭据；旧报告保持兼容。不会 stat/open 等待挂载的目录，不执行 mount/systemctl 命令，不修改 fstab。刷新页面和采集指标均不会触发按需挂载。自定义 systemd unit 及网络文件系统不在本次配置发现范围。

本次经用户授权，服务器已有的 `mnt-backup1.mount`、`mnt-backup2.mount` 已启动并核对设备 UUID、ext4 类型和容量；保留原有 fstab 按需挂载策略，未更改为开机立即挂载。部署按任务分支本地验证、推送、服务器精确 SHA 同步执行，仅重启 XDocker；两块盘上的业务文件不写入。

## 独立 SMART 温度采集（2026-11-15）

常规采集器和网页服务继续使用普通部署用户。对于已明确验证的 NVMe / ATA / USB SAT 设备，可另行安装 `xdocker-smart.service` 和每分钟运行的 `xdocker-smart.timer`。此服务只运行 `/usr/local/lib/xdocker-smart/<固定提交>/smart_sensors.py` 的 root-owned 独立副本，`/usr/bin/python3 -I` 不导入用户工作区。更新普通 checkout 不会改变提权服务运行的代码；升级必须重新审查并安装固定版本。

管理员确认具体设备后，用 `scripts/smart-monitor-unit.py config --device nvme0n1:nvme --device sda:sat …` 生成私有暂存配置。生成器选择 `/dev/disk/by-id/` 稳定标识，拒绝分区、任意路径、自动类型探测和自定义 SMART 参数。设备别名可能含序列号，配置不得输出到聊天或提交 Git。安装配置到 root:root 的 `/etc/xdocker-smart/devices.json`（0600，父目录 0700）；用同脚本的 `service --config <暂存文件> --sha <已推送完整SHA> --group <部署组>` / `timer` 渲染并审查 unit 后，再由管理员安装、`systemd-analyze verify`、daemon-reload、启动服务并启用 timer。

服务使用 `DevicePolicy=closed`，仅对批准的稳定设备路径配置 `DeviceAllow=… r`；`NoNewPrivileges`、严格只读系统、禁止网络地址族、限制内存/任务数、25 秒总超时，仅允许写 `/var/lib/xdocker-smart/`。不授予普通用户 disk 组、不修改 sudoers，也不把 Admin 改为 root。安装前必须核对程序及各级父目录均为 root 拥有且不可由非 root 写入；不能从用户可写 checkout 长期执行提权 Python。

SMART 命令固定为 `-j -d <已批准类型> -H -A`。ATA/SAT 额外固定 `-n standby,3,5`，休眠或无法确认电源状态时跳过，不执行类型探测、写参数、自检或唤醒。每次最多 8 台设备，每台 2 秒；不支持的桥接器保留未知，不盲目尝试其他类型。NVMe 不使用 ATA 电源模式检查。参数和退出位定义见 [smartctl 手册](https://manpages.ubuntu.com/manpages/noble/man8/smartctl.8.html)。

只发布清洗后的温度、SMART 状态、历史告警分类、读取时间和设备代际标识到 `/var/lib/xdocker-smart/report.json`（root:部署组 0640，父目录 0750 且不可由部署组写入）。不发布 SMART 原文、序列号或配置。普通采集器拒绝非 root 所有、可写、符号链接、过大或过期缓存，并核对本次启动 boot ID、当前 sysfs 设备身份和 diskseq；超过 180 秒、重启或设备替换后不冒充当前温度。

页面区分休眠、权限不足、工具缺失、不支持、超时、读取失败、缓存过期及未采集。SMART 当前故障与历史阈值/日志记录分别显示；例如历史温度越阈值显示为历史告警，不能仅凭总体 `passed=true` 隐藏。现有挂载、I/O、GPU 和进程监控不依赖 SMART 成功。无数据库迁移。

部署前备份 XDocker registry、部署回执及已有 unit/config（初次安装须确认路径尚不存在）。本地测试、桌面/窄屏浏览器验收并推送后，服务器同步精确 SHA，再安装 root-owned 副本、启动独立采集服务并核对权限与缓存；仅重启 XDocker 载入新解析。回退先停用 SMART timer/service，再回到部署前已推送的 XDocker 提交并重启自身服务；保留私有缓存及备份，不操作业务数据、挂载、Docker daemon 或其他服务。

## 两块备份盘的空闲停转（2026-11-16）

此功能是主机的独立 systemd 策略，仅在管理员明确选择两块 SAT 备份盘并验证停转、正常访问唤醒后安装。主容器、业务项目和挂载配置无需修改。SMART 私有配置中仅给这两项增加 `idle_pause_seconds: 60`；`scripts/standby-unit.py --config <私有配置>` 渲染 `xdocker-standby.service`，使用稳定 by-id，默认其他盘停转时间为 0，两块选中盘各为 1800 秒。策略不运行备份、不卸载文件系统、不执行 ATA SLEEP；正常文件访问唤醒 standby，首次读取可能等待数秒。已有写入期间不得进行手动停转测试。

该 unit 固定使用 [adelolmo/hd-idle v1.21](https://github.com/adelolmo/hd-idle/tree/v1.21) 的 ATA 模式。Ubuntu Noble 自带的 C 1.05 不支持 `-c ata`，不能混用参数；本次双盘 USB 盒的 SCSI STOP 测试不能可靠隔离目标盘，ATA STANDBY 测试已验证指定盘停转且另一块保持活动。部署须下载固定上游版本、记录源码提交及构建后二进制 SHA256，并把二进制安装为 root-owned `/usr/local/lib/xdocker-standby/hd-idle-1.21`；不得从用户可写目录以 root 长期执行。不要同时启动发行版的全盘 `hd-idle.service`。unit 只授权选中两台设备，绑定对应 device unit，设备消失时退出；重新插入后需核对别名并重新启动策略，开机由 multi-user.target 启动。

SMART oneshot 只读取内核 `/sys/class/block/*/stat` 判断空闲，连续无 I/O 60 秒后暂停选中盘的 SMART 命令，包括电源状态查询；后续实际读写才恢复采集。活动盘仍使用固定 `-n standby,3,5`，不启动自检。原始计数、单调时间和设备代际仅保存在 root-owned 私有缓存，同一启动/同一代际才复用；计数变化、设备替换和重启重新判定。暂停状态每轮更新观测时间，页面明确显示“空闲，暂停 SMART 采集”和有时间戳的“上次温度”，不把上次读数冒充当前温度，也不凭无 I/O 宣称已休眠；上次 SMART 历史告警或故障继续可见。普通采集器读取此缓存时不再额外执行 SMART 回退查询。

验收分别记录：手动停转后已有文件的直接读取、内容一致且文件未修改、另一盘保持活动；真实 30 分钟无 I/O 后 daemon 日志和一次电源状态核对；正常访问后的唤醒；systemd enabled/active 与设备权限。缩短超时测试不能代替真实 1800 秒验证，未实际重启机器时不得声称完成重启验收。回退先停用并禁用 `xdocker-standby.service`，恢复原 SMART 私有配置、固定版本 helper 和 unit，再恢复 timer；无需修改分区、fstab、业务目录或数据库。备份、设备别名及测试文件名不提交 Git。

## Ultra Touch 的 UAS 透传限制（2026-11-18）

实机 Seagate Ultra Touch HDD（USB `0bc2:2065`）使用 Linux 6.8 的 `uas` 驱动。该内核在 `uas-detect.h` 为 Seagate USB 设置 `US_FL_NO_ATA_1X`，`uas.c` 拒绝 ATA 12/16 字节透传，返回 invalid CDB。这使 `smartctl -d sat` 的识别/属性读取失败，电源模式检查甚至可能在一次成功的数据读取后仍报告 SLEEP。Seagate 官方 openSeaChest v26.03.2 的只读 deviceInfo/SATInfo 在本机也未取得温度；它把温度标为 Not Reported，不能用通用 SCSI 返回的 0°C 代替真实温度。

固定采集 helper 仅对这一已验证 USB ID、当前 `uas` 祖先驱动及未覆盖此保护的情况返回 `transport-blocked`，在发出 SMART 命令之前短路。识别只读 sysfs；不读取挂载目录、打开物理设备或修改内核参数。匹配的显式 `usb_storage.quirks` 参数按内核 `t` 标志判定；转换为 `usb-storage` 驱动后恢复原固定 SAT 读取路径。其他 USB ID、NVMe 和两块备份盘不受影响。缓存和 API 保留既有白名单、设备代际、上次读数及历史告警，不推断磁盘当前处于休眠或健康。页面列表与详情显示接口限制和电源状态未确认，在其他资源分区也保留提示。

真实温度恢复需要单独维护窗口：先确认 `/mnt/archive` 的实际源、稳定 by-id、唯一 USB 接口、无占用进程和无进行中写入；备份原模块参数、modprobe 配置、挂载/自动挂载状态及部署回执。只针对 `0bc2:2065` 合并 `usb-storage.quirks=0bc2:2065:u`，临时停止该盘的 automount 并正常卸载，重绑该接口到 usb-storage，再恢复原挂载方式。禁止强制卸载、卸载整个 uas/usb_storage 模块、操作其他盘或自动重启主机。若任一步失败，恢复原参数/驱动和原挂载状态后停止；必须在真实 SAT 温度读取和缓存/API/UI 核对成功后才宣告温度恢复。切换有短暂离线和性能取舍，需获得明确的维护授权，本文和页面修正不代表该切换已经执行。

依据：[Linux 6.8 uas-detect.h](https://github.com/torvalds/linux/blob/v6.8/drivers/usb/storage/uas-detect.h)、[uas.c](https://github.com/torvalds/linux/blob/v6.8/drivers/usb/storage/uas.c)、[Seagate USB compatibility](https://github.com/Seagate/openSeaChest/wiki/USB-Bridge-Limitations)。厂商诊断包固定为 v26.03.2 Linux x86_64 portable，归档 SHA256 `f6f97003e12059cc9176f8d6e56151180cc914475d3228fe51ed3c09c1eb4943`，Basics 二进制 SHA256 `03920bad347c4b52f64ebfe964ec204412fdc5e1c0a84528aed63bb79c4d6a1a`。仅安装 root-owned 诊断副本并使用已审核的只读选项，不加入定时器或给主服务增加设备权限。部署先备份自身数据，按精确已推送 SHA 更新 helper 和 XDocker；软件回退恢复上一 helper/unit/提交，不改变归档盘驱动。
