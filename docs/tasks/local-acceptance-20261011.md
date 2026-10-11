# P0–P3 本地真实容器验收与交付准备

日期：2026-10-11。仅 XDocker；没有修改 PowerX 或实际服务器。

## 镜像与来源

- Docker endpoint 已确认是本机 `desktop-linux` Unix socket；Engine 27.5.1，Linux/ARM64。
- Git 基线为 `10df5936d80f1bdc5e19eb1b8ffbcf2c9fdbe252`，本轮 P0–P3 尚未提交。
- 从当前应用代码构建本地镜像 `xdocker-admin-acceptance:local-4c48e31c0b0e`。
- 应用构建上下文 SHA256：`4c48e31c0b0e9d3c18e55784ed351ac78debab012412046e7078951abfb778a6`。
- 镜像 ID：`sha256:34ab3b1104378e128704b3886a45ac75f0cd9b41735ec90fae51c12947fb4ccc`。

构建上下文只包含 Dockerfile、requirements、指定应用模块与三份静态资源；没有复制服务器配置、令牌或私钥。镜像仅在本机，没有 GHCR 推送，也没有把未提交代码伪装成该 Git 基线的已发布镜像。

## 已执行与结果

| 验证 | 环境 / 方法 | 实际结果 |
| --- | --- | --- |
| 镜像构建 | 当前未提交应用代码，原 Dockerfile，原生 Linux/ARM64 | 成功；Python 3.13/依赖及全部新模块打入镜像 |
| 真实 Docker 管理 | `tests/admin_smoke.py`，独立命名测试容器与临时管理状态 | snapshot、日志、CPU/内存、只针对测试 CID 的停止/启动/重启、退出登录通过 |
| 私有配置读取 | 临时配置文件 600、固定测试挂载、已登记随机测试实例 | 白名单信息、单项密码读取、停止容器后的只读读取通过；审计不含密码，配置原文未被修改 |
| 实际 Gunicorn HTTP | 候选镜像默认启动命令；非 root UID、只读根文件系统、cap_drop、no-new-privileges、临时回环端口 | 健康、匿名 401、登录/会话/退出及 no-store 通过；新 API 可以访问 |
| 默认行为 | 实际镜像中的 P1/P2/P3 API | Docker Engine 检查通过；没有宿主机报告时保持 unknown；调度关闭、候选目录为空；未登记实例返回 404 |
| 静态资源打包 | HTTP 获取 HTML/JS/CSS，与本地对应文件计算 SHA256 比较 | 三份资源逐字节匹配；不是浏览器点击验收 |
| 完整维护备份 | `tests/backup_smoke.py`；一次性 PostgreSQL/Redis/前后端，样例数据 | pg_dump、Redis RDB 检查、配置/文件打包、组合校验通过，前后端恢复，样例业务数据保留 |
| 隔离数据库恢复 | 无网络、无公网端口的临时 PostgreSQL | pg_restore 成功；没有覆盖测试运行库或其他数据库 |
| 计划备份集成 | 一次性实例；测试用计划和注入时间，经实际 app 调度准入及 Docker/数据库执行 | 默认计划禁用；明确测试准入后完整备份校验与服务恢复成功；同一时间点不重复，禁用成功，样例数据保留 |
| 清理 | 只删除本轮记录的测试容器 ID，临时目录自动清理 | 没有残留本轮测试容器或一次性实例；本地测试镜像和构建缓存留作复核 |

计划备份验证使用可丢弃的数据及测试注入时间，不是对真实业务实例开启每天计划或等待真实 03:00 定时线程触发。产品/服务器调度仍保持默认关闭。

最初 HTTP 与计划集成使用临时脚本；本轮已整理到仓库 `tests/admin_http_smoke.py`、`tests/backup_smoke.py` 并重新通过实际容器验证，尚未提交。fixture 令牌只用于临时服务，验收记录不保存这些值。

## 仍未验收

- Linux/AMD64 候选镜像的真实运行，以及同一提交的双架构发布。
- 真实 macOS LaunchAgent 安装/SSH 中断恢复；现有手动 SSH、FRPC 未被改动。
- 浏览器实际登录、交互和连接恢复；HTTP/事件模拟不替代浏览器验收。
- PowerX 新 Release/私有 registry 与正式发布契约；没有登记虚构候选。
- 目标服务器同步、镜像拉取、Admin 更新、真实计划启用或业务停机。

## CI 交付准备：本地实现完成

改造前 `.github/workflows/publish-admin.yml` 有两个交付缺口：

1. 只有 main push 和 workflow_dispatch，没有 PR 校验入口；任务分支 PR 尚无这套自动验证。
2. `docker/build-push-action` 的 `push: true` 在真实容器 smoke 之前执行。失败时虽可阻止部署，但 registry 里已经出现尚未完整验收的 SHA 镜像。

用户确认本地改造范围后，已完成：

- PR/main 检查入口、只读全局权限、AMD64/ARM64 原生构建与 smoke 矩阵；候选阶段不推送。
- HTTP/生命周期/完整备份/隔离恢复/一次性计划准入整理为仓库脚本；构建上下文使用白名单，并实际验证根目录及静态资源子目录的假私有文件不会进入镜像。
- `scripts/admin_image_artifact.py` 保存 Docker tar 与回执，在发布前核对双架构归档 SHA256、源码 SHA、平台及镜像 ID，加载后再次核对；发布阶段不重新构建。
- 发布与制品上传仅允许原仓库 main push/main 手动运行，PR/任务分支手动运行/fork 均被挡住。仅发布 job 获得 GHCR 写权限；同一分支的 push/手动任务串行。
- 保留完整 SHA 索引标签及架构标签，发布结束校验索引双架构；[管理指南](../guides/admin/README.md#发布镜像)说明入口、结果与失败处理。

本轮实际验证：167 项 Python 回归通过（含新增 12 项交付门禁/制品校验），前端事件模拟通过；[actionlint v1.7.12](https://github.com/rhysd/actionlint/releases/tag/v1.7.12) 对本工作流检查通过。HTTP、Docker 生命周期、备份/隔离恢复/计划准入、真实构建上下文过滤均通过；本机 ARM64 镜像包导出、归档校验、重新加载后的镜像 ID 与原候选一致。中间测试 tar 已清理，没有残留本轮测试容器。

上述本地实现阶段没有提交、推送、创建 PR、合并或触发 GitHub 工作流，也没有 GHCR 推送和服务器操作。AMD64 原生执行与双架构交付仍待真正的 GitHub CI 验收；本机 ARM64 结果不冒充这两项通过。实现授权与提交/推送/发布/服务器操作授权分别处理，遵守 [AGENT.md](../../AGENT.md)。

## 后续 PR 交付范围

用户已授权推进交付第 1 步，使用独立分支 `feat/admin-delivery-20261011` 提交、推送、建立面向 main 的 PR 并跟进 CI。本节记录授权范围，不预先宣称 GitHub CI 通过；实际工作流/平台结果以该 PR 的检查记录为准。此阶段不合并 main、不启用自动合并、不发布 GHCR 镜像、不登录或更新服务器，计划备份仍默认禁用。
