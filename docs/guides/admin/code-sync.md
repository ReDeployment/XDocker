# 代码同步：只推送已有提交

入口：**研发协作 → 代码同步**（`/#code-sync`）。源码仓库、推送计划、目标设备、分发策略和执行记录使用五个任务分区，编辑保留草稿，错误与未确认任务在分区之外持续显示。

## 当前规则

选择来源目录当前所在的开发分支，将该分支的**已有提交原样推送到 GitHub 同名分支**。保留 commit SHA、作者、提交描述和历史；不创建快照提交，不强制 `xdocker-backup/` 前缀，不执行 git add、commit、checkout、reset 或强推。远端分叉、来源 HEAD 变化或浅克隆时停止，由项目维护者处理分支。

未暂存、已暂存和未跟踪的工作区改动只显示待处理数量；它们不进入推送，也不会被自动提交。开发者或对应项目 Codex 先完成正式提交及描述，定时任务负责推送和副本同步。自动提交工作区和目录白名单模式本版本未开放。

## 推送前检查

使用私有 bare cache 读取 Git 对象，扫描远端当前 SHA 到本地选定 HEAD 之间**所有待发送提交**的新增/修改文件、提交元数据，包含合并分支及曾加入后又删除的内容。来源工作区、暂存区和 refs 均保持不变；GitHub 凭据、目标 URL 和 Git 配置与项目自己的配置分开处理。没有新提交时不生成新 commit。

规则检测常见私钥、GitHub/云/API 令牌、带密码的连接地址、疑似明文凭据、已提交的环境/临时/数据库文件，以及无法扫描的二进制、LFS 外部对象、符号链接和子模块。样例环境文件允许明确占位符，但仍检查内容。识别的常见图片/字体可作为二进制资源检查。报告仅提供文件、提交、规则及行号，不回显命中值或片段；含风险的提交描述隐藏。

这些规则是启发式检查，不保证发现所有敏感数据，不能替代项目提交审查。单文件 8 MiB、检查内容总计 64 MiB、最多 10,000 个文件版本、500 个待发送提交；元数据/子进程输出、缓存和对象传输同样有界。命中、无法检查、范围超限或读取失败即停止，不自动略过风险文件后继续推送，因为略过会改变原提交 SHA。新远端分支可能需要检查全部本地历史，过大时由项目先人工审查和发布。

页面「推送预览」在有凭据时核对远端并检查完整待推送范围；没有凭据时仅检查本地当前提交，明确标为尚未核对远端。执行前重新检查，预览不授权重放已变化的计划。

## 配置与运行

1. 管理员批准源码根目录、固定 Agent 路径和 SSH 身份。策略 0600、目录 0700；使用 `policy-example`、`policy-import`。私有策略、令牌与密钥不入 Git。
2. `bootstrap-workspace` 发现主 GitHub 仓库，复用项目登记，按 origin 去重并排除辅助 worktree/autobackup。来源及默认每日 02:00 Asia/Shanghai 计划均关闭。detached HEAD 仅展示，需由项目选择分支后才能开启推送。
3. 在源码仓库配置 GitHub 写入凭据，预览提交与待处理改动，确认来源分支并开启；在推送计划中确认开启每日推送或仅同步已确认提交。
4. 目标设备使用 VPN / 内网 IP、固定 SSH 身份和可信渠道核对的主机公钥。安装同版本 Agent，使用 **schema 2** 私有策略批准 `directory`、`repository`、`branch`、读取凭据及可选服务。当前支持 Linux/macOS，Windows Agent 尚未支持；重启仅支持 systemd。
5. 分发策略启用前核对设备端授权；只更新到 GitHub 已回读确认的准确 SHA。脏工作区、忽略文件碰撞、分支分叉、授权漂移均停止；不 reset/clean/强制覆盖。仅在副本实际更新后重启明确批准且定义固定的服务。

```sh
python3 scripts/code-sync.py policy-example --state data/native-admin
python3 scripts/code-sync.py policy-import --state data/native-admin --file /absolute/private/policy.json
python3 scripts/code-sync.py bootstrap-workspace --state data/native-admin
python3 scripts/code-sync.py agent-example --state data/native-admin
```

调度器在原生 Web 热更新进程之外，以单进程锁运行。执行要求原生 operations 与私有 policy.execution 同时开启。Docker Admin 可管理配置及预览，本版不在 Docker Web 进程内调度。同步不替代项目自己的构建、数据库迁移和部署验收。

## SQLite 升级、记录与恢复

`data/native-admin/code-sync/control.sqlite3` schema 2 保存配置、计划、队列与回执；不修改应用登记或业务数据库。升级需要 SQLite 3.35+，部署前用旧版本的 `scripts/code-sync.py backup` 备份旧库。schema 1 升级保留来源分支、稳定标识和历史，移除旧快照范围/备份分支字段，关闭全部来源、计划和目标，并使旧目标授权和排队任务失效，等待重新核对。升级不会自动切换业务分支或触发推送；Agent v1 授权不能直接复用。

推送前持久化候选 SHA，推送后回读 GitHub，再分发。服务/上传结果未知时不自动重放；未确认任务阻断同项目后续任务，需在记录中核对或明确结束跟进。历史归档保留幂等回执与未确认任务。源主库最多 300 条活跃记录，270 条起逐批归档已结束任务；上限 1000 批归档，Agent 上限 10,000 条历史回执，超限时停止并提示。

SQLite 一致备份使用 `backup --file <不存在的路径>`。完整恢复还需该私有目录中的策略、凭据与回执。回退到 v1 时不要让旧执行器读取升级后的库；保留原备份并保持计划关闭。
