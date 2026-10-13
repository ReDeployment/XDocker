# 六项目 GitHub 观察与通知

状态：2026-10-12，本地实现，未提交/发布/部署。API 轮询观察已实现，另已补齐 [独立 Webhook 接收与通知](github-webhook.md) 的本地代码；不会操作其他项目、提交评论、修代码、重跑 CI、合并 PR、构建或发布镜像。

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

1. “读取缓存”只读取服务器观察记录，不请求 GitHub。“立即采集全部”或项目行的“采集”才启动一次 GitHub 只读采集。
2. 项目卡显示实际采集状态、最近成功时间、分支完整 SHA，以及检查/审查的观察结论。详情包含 PR、检查、失败 job/step、代码注解的路径/行号和 GitHub 原文链接。
3. 通知收件箱显示新发现或发生变化的状态、评论/审查及其提交关系。首轮只建立基线通知；历史评论原文以纯文本显示，不渲染其 HTML、不作为执行指令。
4. “已读”只更新本地收件箱，不解决 CI 失败、不关闭 GitHub 评论线程，也不授权发布。
5. “观察配置”支持逐项目禁用、分支和检查名称。空分支列表使用仓库默认分支；分支最多三项。检查名为空观察全部，非空则评估指定名称；缺少指定项保持未确认。保存采用记录版本冲突保护，变更后旧快照不作为当前配置的结果。

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

服务器不会自动继承本机 gh/keyring。默认使用固定 GitHub REST API GET，凭据按 owner 分开读取：

```text
data/admin/github/credentials/artisancloud.token
data/admin/github/credentials/matrix-x.token
```

目录私有，token 文件必须非符号链接、`600` 或 `400`；不提交 Git、不放入镜像或网页。没有 token 可尝试匿名公共资源，私有资源/限流保持未确认，不据 404 断言仓库不存在。采用支持这些仓库的只读凭据：Metadata、Contents、Actions、Checks、Pull requests、Issues 的读取范围；不同 owner 的细粒度授权独立配置，避免假定一个安装/owner 的权限自动覆盖另一个。

凭据只用于采集 API，不向 Docker、PowerX、网页或日志传递。不在 URL 中携带 token，不跟随仓库迁移重定向，也不接受网页上传凭据。私有评论只供已登录管理控制台查看，保存前做常见凭据与当前 token 的脱敏；仍不建议在 GitHub 评论粘贴秘密值。

Admin 运行设置：

```dotenv
ADMIN_GITHUB_WATCH_ENABLED=false
ADMIN_GITHUB_WATCH_INTERVAL=300
```

默认关闭自动采集，各项目仍可人工只读采集。获得运行配置/部署授权后才可启用总开关。周期范围 60–3600 秒，建议先保留 300 秒；自动模式首次等待一个周期，错过的时间点不补跑。进程锁防止多实例自动/手动采集重叠；禁用不强制中止正在进行的读取，配置变更期间返回的旧结果不作为新配置快照。

每项目独立进程隔离，网络采集有 45 秒内部预算及 50 秒进程上限；失败一个项目继续其余项目。限流/Retry-After 遵守冷却时间，网络/权限错误保留旧快照并标为未确认，不反复生成相同错误通知，不把旧绿色结果当成当前通过。

## 本地 gh 验证方式

在已安装 Admin Python 依赖的环境，明确使用现有本地 gh 认证：

```sh
python3 scripts/github-watch.py \
  --state /你的私有临时目录 \
  --project PowerXPlugin --gh
```

该命令只调用 `gh api --method GET`，输出项目/采集状态，不打印评论正文、响应或凭据；退出 0 表示采集成功，退出 2 表示未确认。它在指定目录保存私有观察记录，不启用服务器自动采集，也不使用本机 Docker。可选项目为上表名称或 all。

本轮真实只读 CLI 尝试出现 GitHub API EOF，结果为 github-cli-unconfirmed；未声明持续线上采集验收通过。

## 数据与覆盖范围

记录位于 `data/admin/github/watch.sqlite3`（容器内 `/state/github/watch.sqlite3`）：逐项目设置、快照、去重状态、通知与已读标记。目录 `700`，数据库 `600`，未知 schema/损坏或符号链接保持失败，不覆盖原文件。独立于 Admin 既有 jobs/audit 数据库及 PowerX 业务库。

观察窗口为最近 10 个 PR、每类最近 30 条评论，以及配置分支与最多三个开放 PR 的提交；审查/检查/任务列表上限 100，未取全保持未确认。缺少窗口中的 PR 关联时，不把普通 issue 评论猜成 PR 评论。新近变化每次最多入库 100 条；收件箱不是完整 GitHub 审计日志。

已读旧通知超过最近 2000 条可清理，未读保护；总记录达到 10000 时保持 inbox-full，不静默丢掉未读通知，需要处理已读记录后再采集。首轮不把全部历史评论作为新事件，也不声称捕获所有短暂编辑/删除或每一次 push。

独立 Webhook 接收器已在本地实现，签名验证、delivery 去重、队列导入重试及 GitHub 手工重投见 [配置指导](github-webhook.md)；保持 Admin 私有入口，不将 Docker socket 暴露给接收器。本轮尚未配置任何 GitHub Webhook 或对外通知渠道。

实现参考：[PR API](https://docs.github.com/en/rest/pulls/pulls)、[Reviews API](https://docs.github.com/en/rest/pulls/reviews)、[Workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs)、[Issue comments API](https://docs.github.com/en/rest/issues/comments)。
