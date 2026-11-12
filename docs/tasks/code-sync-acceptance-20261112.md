# 代码同步验收记录

2026-11-12；任务分支 `codex/host-resources-20261112`，沿用既有原生网关与资源监控部署。

实现：独立 SQLite 配置与队列，五分区 UI、来源快照预览、GitHub 独立备份分支、每日计划、固定 VPN/SSH Agent、副本只快进与批准服务可选重启、未知结果保护、幂等与归档。默认不开启来源或计划。

本地验收包含真实 Git 快照上传到临时 bare remote，再经 CopyAgent 克隆及只快进，来源 HEAD/index 保持原样；覆盖密钥排除、SQL 源码保留、文件碰撞、脏工作区、分支分叉、策略漂移、一次性重启、未知上传核对、恢复、归档幂等、认证与 CSRF。真实浏览器在桌面 1440×1000、窄屏 390×844 检查五分区、草稿、版本冲突、凭据不回显、可见操作与持续错误。SSH 与 systemd 使用隔离适配器；没有真实 GitHub 写入或业务服务重启。

已部署到 `home-pc-ubuntu:/data/workspace/XDocker`；应用沿用 `xdocker-native.service`，执行器位于 Gunicorn 热更新进程之外，调度心跳在线。入口 `http://xdocker.home.test/#code-sync` 实际登录与桌面/窄屏五分区验收通过，无 JS 例外；编辑草稿只在浏览器核验，未保存真实配置或创建运行任务。

workspace 导入 7 个主仓库：PadelU、PadelScoreboard、Deploy、PowerXDocs、XDocker、PowerXPluginScora、PowerXPluginScoraMP。复用 padel-scoreboard、powerx-docs、xdocker-admin 登记 ID；来源与每日 02:00 Asia/Shanghai 计划全部关闭。相同 origin 的 PowerXDocs 临时修复克隆已去重；仅移除了本次新建、无凭据、无目标、无任务的关闭配置，并保留配置回执与 SQLite 备份，未删除或改动来源目录。Scora 来源为 detached HEAD，固定准确 SHA，未切换业务分支。

未配置 GitHub 写入凭据、SSH 身份和目标设备，运行任务为 0。没有执行用户仓库上传、设备副本同步或业务服务重启。既有 XDocker、scoreboard、docs 三个域名实际 HTTP 200。服务器专项测试与实际 Nginx 隔离验证通过；GitHub CI 包含真实 Docker 构建。准确部署提交、备份位置与校验在主机私有 deployment-receipt.json 中记录。

恢复边界：原有 registry/session/metrics SQLite 保留；新功能使用独立 code-sync 数据库。回退到前一原生版本不删除新库或回执，不操作业务项目。
