# 六项目 GitHub 观察与通知

本指南说明六项目 API 观察、Webhook 通知与新版自动采集；Release、问题处理和证据诊断详见 [通知与处理指南](github-triage.md)。

## 项目范围

| 控制台名称 | 固定仓库 |
| --- | --- |
| PowerX | ArtisanCloud/PowerX |
| PowerXPlugin | ArtisanCloud/PowerXPlugin |
| PowerXPluginSCRM | ArtisanCloud/PowerXPluginSCRM |
| PowerXPluginECommerce | ArtisanCloud/PowerXPluginECommerce |
| PowerXPluginMediaXStudio | Matrix-X/PowerXPluginMediaXStudio（私有） |
| PowerXPluginCRM | Matrix-X/PowerXPluginCRM（私有） |

映射已通过 GitHub 元数据核对；SCRM/ECommerce 的旧本地 remote 不作为当前规范地址。CRM 地址由用户确认并通过 API 核对。网页选择固定项目，不接受任意仓库/API URL、SQL、shell 或执行指令。

## 页面怎么用

登录 XDocker，进入“研发事件”（`/#github`）。管理入口继续使用既有 SSH 隧道，不新增公网端口。

1. 完成一次 [GitHub 只读连接](github-connection.md) 并启用自动更新后，首次检查、近期历史补入、新事件核对和定时补漏都在后台运行，无需逐个点击。
2. 默认看「待处理问题」，再到「项目检查 → 查看详情」核对 PR、失败步骤、机器人评论和来源。项目行直接说明等待连接、正在检查、失败或需要复核。
3. 详情始终显示该项目最近收到的消息，不依赖全局通知分页；没有完整检查时明确说明原因，不把空内容当作检查通过。
4. 「刷新页面」只读取保存数据。「高级操作」中的手动重试、历史补采集只用于排查；「监控设置」用于分支、检查名及逐项目禁用，配置采用版本冲突保护。
5. 「已读」只更新本地收件箱。问题处理状态、CI 结果与发布授权分别保存。评论纯文本显示，不执行其中命令。

Docker snapshot 不可用时仍可查看/管理观察数据，前提是 Admin API 和认证连接可用。离线、会话或 CSRF 异常时停止提交；状态轮询只有 GET，不会重新发送采集/配置/已读请求。

## 如何理解结论

| 结论 | 含义 |
| --- | --- |
| 需处理 | 对应 SHA 的所观察检查失败，或当前提交存在 changes_requested |
| 检查执行中 | 当前检查未完成，不能把上一轮成功当成这次结果 |
| 审查待确认 | 开放 PR 尚无当前提交批准，或旧提交仍有需重新核对的修改要求 |
| 观测检查通过 | 本轮所观察的检查通过；不代表足够的审批、实际镜像验收或发布授权 |
| 未确认 | 检查缺失/跳过、响应或分页不完整、采集异常、配置改变、已禁用或缓存超过新鲜范围 |

每个提交分别读取 checks、commit statuses 与 workflow runs；同 workflow 的新 run/attempt 覆盖旧失败，不以仓库最新一条绿色结果代替另一 SHA。失败时记录 job/step 和可用的代码注解，不自动下载完整日志。

PR 审查区分 approved、changes_requested、commented、dismissed 及提交关系。Commented 或机器人的“Completed”文字不等于批准；旧 SHA 的修改要求保留为待复核。普通 PR 评论没有明确 commit_id，标为未绑定；行内评论保留原提交、路径和行号。当前未接入 GraphQL review-thread resolved 状态，不能自动把某条评论宣布为已解决。

所有输出固定 `publish_authorized=false`。若希望真实阻止镜像构建，需对应项目的 Codex 在其 Actions 中配置构建前的静态/单元检查和审查要求；真实安装/升级/恢复验收在构建后、发布前完成。监控 UI 本身不会修改这些工作流。

## 认证与定时轮询

新接入使用 [GitHub App 与自动续取](github-connection.md)。App 配置优先读取 `data/admin/github/credentials/app.json` 与其私钥引用；以下 owner token 文件仅为旧方式兼容。仓库 API 读取始终 GET；App 申请短期令牌使用固定认证 POST，不修改仓库。

服务器不会自动继承本机 gh/keyring。默认使用固定 GitHub REST API GET，凭据按 owner 分开读取：

```text
data/admin/github/credentials/artisancloud.token
data/admin/github/credentials/matrix-x.token
```

目录私有，token 文件必须非符号链接、`600` 或 `400`；不提交 Git、不放入镜像或网页。自动模式缺少 token 时显示等待连接，不反复匿名请求。显式手动读取可尝试公共资源，私有资源/限流保持未确认，不据 404 断言仓库不存在。App 需要 Metadata、Contents、Actions、Checks、Commit statuses、Pull requests、Issues 的读取范围；同一个 App 分别安装到两个 owner，每次只为当前所属方的单个固定仓库发放只读安装令牌。旧 PAT 的 Checks 支持限制见连接指南，不保证仅凭 PAT 完成采集。

凭据只用于采集 API，不向 Docker、PowerX、网页或日志传递。不在 URL 中携带 token，不跟随仓库迁移重定向，也不接受网页上传凭据。私有评论只供已登录管理控制台查看，保存前做常见凭据与当前 token 的脱敏；仍不建议在 GitHub 评论粘贴秘密值。

Admin 运行设置：

```dotenv
ADMIN_GITHUB_WATCH_ENABLED=false
ADMIN_GITHUB_WATCH_INTERVAL=300
```

默认关闭自动采集，完成 App 只读连接并获得运行配置/部署授权后启用一次。周期范围 60–3600 秒，建议保留 300 秒。新版启动即安排首轮近期历史检查，Webhook 导入后安排对应项目刷新（15 秒合并、30 秒最小间隔），周期检查补漏；队列持久化，进程重启后继续。初始历史超过单次 100 条时自动继续，不要求用户逐个点击。进程锁防止多实例自动/手动采集重叠；禁用不强制中止正在进行的读取，配置变更期间返回的旧结果不作为新配置快照。

每项目独立进程隔离，网络采集有 45 秒内部预算及 50 秒进程上限；失败一个项目继续其余项目。限流/Retry-After 遵守冷却时间，其他错误指数退避 60 秒至 1 小时；网络/权限错误保留旧快照并标为未确认，不反复生成相同错误通知，不把旧绿色结果当成当前通过。

## 本地 gh 验证方式

在已安装 Admin Python 依赖的环境，明确使用现有本地 gh 认证：

```sh
python3 scripts/github-watch.py \
  --state /你的私有临时目录 \
  --project PowerXPlugin --gh
```

加上 `--backfill` 可显式补入观察窗口内的历史通知。加 `--gh` 时只调用 `gh api --method GET`；默认使用私有认证配置，App 方式先申请安装令牌再执行仓库 GET。命令输出项目/采集状态，不打印评论正文、响应或凭据；退出 0 表示采集成功，退出 2 表示未确认。它在指定目录保存私有观察记录，不启用服务器自动采集，也不使用本机 Docker。可选项目为上表名称或 all。

前一轮本机 gh 六仓库真实只读采集成功，记录见任务文档；不代表服务器已配置凭据或已启用新版自动检查。

## 数据与覆盖范围

记录位于 `data/admin/github/watch.sqlite3`（容器内 `/state/github/watch.sqlite3`）：逐项目设置、快照、去重状态、通知与已读标记。目录 `700`，数据库 `600`，未知 schema/损坏或符号链接保持失败，不覆盖原文件。独立于 Admin 既有 jobs/audit 数据库及 PowerX 业务库。

观察窗口为最近 10 个 PR、每类最近 30 条评论，以及配置分支与最多三个开放 PR 的提交；审查/检查/任务列表上限 100，未取全保持未确认。缺少窗口中的 PR 关联时，不把普通 issue 评论猜成 PR 评论。新近变化每次最多入库 100 条；收件箱不是完整 GitHub 审计日志。

已读旧通知超过最近 2000 条可清理，未读保护；总记录达到 10000 时保持 inbox-full，不静默丢掉未读通知，需要处理已读记录后再采集。首次自动检查补入观察窗口内近期记录；显式普通 CLI 采集仍仅建基线。也不声称捕获所有短暂编辑/删除或每一次 push。

独立 Webhook 基础接收器已上线，新版 Release 接收仍待发布；签名验证、delivery 去重、队列导入重试及 GitHub 手工重投见 [配置指导](github-webhook.md)；保持 Admin 私有入口，不将 Docker socket 暴露给接收器。六个既有签名 Webhook 已配置；本轮未改变线上回调或外部通知渠道。

实现参考：[PR API](https://docs.github.com/en/rest/pulls/pulls)、[Reviews API](https://docs.github.com/en/rest/pulls/reviews)、[Workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs)、[Issue comments API](https://docs.github.com/en/rest/issues/comments)。
