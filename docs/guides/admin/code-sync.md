# 代码同步

入口：**研发协作 → 代码同步**（`/#code-sync`）。五个分区分别管理源码仓库、上传计划、目标设备、分发策略与执行记录。桌面使用纵向分区导航，窄屏使用横向导航；编辑弹窗保留草稿，执行中断与计划阻断在各分区持续可见。

## 首次配置与日常使用

1. 管理员在主机批准源码根目录、SSH 身份和固定 Agent 路径；参见页面的「连接与授权指南」与 `scripts/code-sync.py policy-example`。策略文件 0600、目录 0700，私有策略和密钥不能提交到 Git。
2. `bootstrap-workspace` 对齐批准根目录中的主 Git 仓库，复用应用登记 ID，排除 autobackup 和未登记 worktree；主仓库下的嵌套副本不会重复登记。仅接受 GitHub origin。每个来源和每日 02:00 Asia/Shanghai 计划都保持关闭，不自动上传。
3. 在「源码仓库」添加 GitHub 仓库写入令牌，选择凭据，预览所选文件，输入完整项目名称开启来源。令牌仅保存在主机私有文件，SQLite 只保存引用，API 不回显。
4. 在「目标设备」填写 VPN / 内网 IP、SSH 用户、已批准的身份引用与从可信渠道核对的主机公钥。设备端需安装同一版本 Agent，并在其私有策略批准副本目录、仓库、备份分支和可选 systemd 服务。当前支持 Linux / macOS，Windows Agent 尚未支持；服务重启仅支持 systemd。
5. 在「分发策略」核对设备端授权并启用副本。在「上传计划」选择每日上传并分发，或仅同步最近一次已核对的提交，然后确认开启。

```sh
python3 scripts/code-sync.py policy-example --state data/native-admin
python3 scripts/code-sync.py policy-import --state data/native-admin --file /absolute/private/policy.json
python3 scripts/code-sync.py bootstrap-workspace --state data/native-admin
python3 scripts/code-sync.py agent-example --state data/native-admin
```

`policy-example` 和 `agent-example` 只输出模板；策略需管理员填写并审核。原生启动的调度器在 Web 热更新进程之外运行，单进程锁避免重复调度。Docker Admin 可管理配置及预览；本版本真正的定时执行要求原生运行并开启 operations 和私有 policy.execution。

## 备份、分发与服务边界

上传的是选中代码的**文件快照**，不改变源目录 HEAD、分支、暂存区；不是把来源历史自动提交到 main/develop。快照历史写入同一 GitHub 仓库的 `xdocker-backup/*` 独立分支，要求该备份分支由同一个来源首次创建。没有文件变化时复用提交。遵循源 Git 忽略规则，并强制排除 .env、常见密钥、数据库文件和根运行目录；SQL 源码迁移文件保留。预览不等于秘密扫描，业务凭据不应写在代码文件内。上限为 10,000 个文件、总计 64 MiB、单文件 8 MiB。

目标副本使用专用备份分支，可首次克隆到批准的空目录，也可更新已有同仓库副本。先检查目标干净、分支正确、授权未变化，再只快进到本次 GitHub 回读确认的准确 SHA；不执行 reset、clean 或强推，有本地修改、忽略文件碰撞或分叉则停止。将开发目录与接收副本分开配置。

重启只在副本实际更新后执行，且只能调用设备端策略中批准并固定定义摘要的 systemd 服务。没有任意 Shell、构建、镜像发布或迁移入口；各项目仍需自己的构建及部署验收。SSH 只信任固定主机公钥，关闭身份代理与用户 SSH 配置继承，令牌不写入 Git URL。

## 存储、记录和恢复

配置、计划、任务与回执保存在 `data/native-admin/code-sync/control.sqlite3`；来源代码只读。私有策略、令牌、bare 缓存及归档在同一 code-sync 私有目录。SQLite 独立建表，不迁移应用登记或业务数据库。

上传前先持久化候选 SHA，上传后回读 GitHub；设备写入前先保存意图并按 request_id 保存不可重复执行的回执。进程中断、网络断开或服务结果未知，不自动重放未知操作。执行记录提供上传结果核对，以及明确的「结束跟进」；结束跟进不会把未确认结果标记成功。确认是否成功应查看准确 SHA 与设备回执，而非仅看排队或命令退出码。

主库最多 300 条活跃记录，达到 270 条后每轮自动归档最多 10 条已结束记录，保留未确认任务。历史归档可在执行记录查看，归档后仍保存幂等索引；Agent 同样保留永久请求回执并滚动归档已核对记录。主机上限 1000 批归档，Agent 上限 10,000 条归档；达到限制会停止并提示，不会删除未知结果。可用 `backup --file <不存在的路径>` 做一致 SQLite 备份；完整恢复还需妥善备份该私有目录中的授权、凭据和回执。

本功能的隔离验收使用真实本地 Git 仓库、bare remote 与 CopyAgent；SSH 传输和 systemd 由 fixture 替代。该证据不代表已使用用户 GitHub 凭据上传，也不代表任何真实业务服务已经重启。
