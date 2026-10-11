# 镜像版本与升级预览

XDocker 左侧 **镜像版本**（`/#image-versions`）读取登记实例的前后端配置、实际容器镜像、候选版本和发布证据。本功能是只读管理：**不拉取镜像、不改 `.env`、不执行迁移、不启动或重启服务**。没有“应用升级”按钮；后续升级须另行报告数据影响并获得授权。

本地实现完成后仍须按授权流程发布、同步与部署 Admin，服务器才会出现新页面。候选目录默认为空；不会根据名字生成一个已发布正式版本，也不会自动追随 develop、latest 或最新 Release。

## 开发与正式通道

- **开发候选**：维护者登记同一源码 SHA 的前后端固定 digest；证据检查要求受支持的完整发布工作流来自 `develop`。
- **正式候选**：维护者登记最终 SemVer Release，如 `v1.2.3`，以及对应源码 SHA 和前后端 digest。在线证据还要核对 Release 不是 draft/prerelease、有完整部署包及校验文件、tag 最终指向该源码提交。
- `prod`/`production` 实例的技术预览拒绝开发候选；测试/开发实例可审查正式候选，但不会自动切换。

SHA tag 也可以被 registry 重指。候选必须用 `name@sha256:<digest>` 固定；当前实例已有 SHA tag 时，页面保留真实配置并显示提示，不替你改成 digest。

当前参考 PowerX 发布流程有 SHA 镜像、部署包和安装 smoke 步骤，但没有供本功能直接导入的统一正式版本/迁移 JSON 契约。**这里的目录是 XDocker 自己维护的登记格式**，不是宣称 PowerX 已产出的发布元数据。未来自动接入格式需由 PowerX 维护者确认；本代理没有修改 PowerX 发布工作流。

## 字段来源与证据范围

| 信息 | 来源 | 如何理解 |
| --- | --- | --- |
| 实例与环境 | 登记的 `instance.json`、实例 `.env` 的 `DEPLOYMENT_ENV` | 只读取登记启用的 PowerX；未确认环境会阻塞预览 |
| 配置镜像 | `.env` 的 `POWERX_BACKEND_IMAGE` / `POWERX_WEB_IMAGE` | 这是配置意图，不能代替实际容器；只输出这些镜像字段，不输出其他私有值 |
| 实际镜像 ID | 验证项目/服务身份后的 container inspect `Image` | 是配置内容 ID；**不能当作 Registry manifest/index digest** |
| 实际 registry digest | 实际镜像的 `RepoDigests` | 缺失时明确显示未知，不用镜像 ID 补填 |
| 源码 SHA | 实际镜像 OCI revision label | 只把完整 SHA label 展示为实际镜像声明；SHA tag 另列为提示，不能代替缺失 label |
| 配置与运行一致性 | 配置引用解析到的本机镜像 ID vs 容器实际 `Image` | tag 重指或只改 `.env` 而没更新容器时可能不一致；不自动修复 |
| 候选目录 | `apps/powerx/versions.json` 或指定的本地目录文件 | 人工登记不等于在线发布验收通过 |
| 本机候选核验 | 指定 digest 的 image inspect、RepoDigests、OCI revision、当前 Engine 平台 | 前后端都须匹配对应 digest、同一候选源码 SHA 和本机平台；网页不 pull |
| 远端清单 | 固定 GHCR 匿名 manifest/token GET | 元数据可读不代表镜像层完整下载；私有权限不足保持需核对，不读取 Docker 登录凭据 |
| CI 证据 | 固定 PowerX GitHub workflow/run/jobs API | 核对 head SHA、完整工作流 success、受支持的前后端发布及安装步骤；不等于你服务器或全部业务验收 |
| 正式发布关联 | 固定 PowerX Release/tag API | 核对正式属性、部署包和源码关联；不下载部署包或把 Release 标签当作内容校验 |
| 迁移/回退 | 候选目录中的维护者声明，绑定当前源码版本 | 不是自动生成的数据库兼容性证明，需要 PowerX 维护者提供具体说明 |
| 历史备份 | 当前登记实例的成功备份清单 | 历史 ready/verified 字段不证明本次字节复核；升级前仍须新建、校验与确认恢复方案 |

只返回上述镜像白名单数据，不返回容器环境变量、Docker 登录文件、PAT、数据库密码或 config.yaml。后台 GitHub/registry 查询不跟随重定向、不接受网页自定义 URL，不配置或读取 GitHub/PAT 凭据。

## 页面操作

1. 选择 **实例**，点击 **核对当前运行版本**。核对前后端实际 SHA 是否成对，以及配置解析的镜像 ID 是否匹配容器。查看读取时间，不能把旧截图当作当前状态。
2. 选择开发/正式候选通道。没有记录时显示“尚无有效候选记录”；当前实例仍可查看。
3. 对候选点击 **核验远端与 CI**：逐项检查后端清单、前端清单、发布证据。GitHub 查询可能受网络、权限或限流影响，失败时显示未确认，不自动反复查询；单次证据子进程最多 25 秒。
4. 点击 **核验本机镜像**：前后端都显示 digest、源码和平台匹配，才算本机候选核验通过。缺少镜像时由你在服务器实际部署上下文决定是否拉取；页面不会运行 pull。使用 `docker load` 导入后如果丢失 RepoDigests 或无法按 digest 解析，仍会显示未确认，不能用已有 tag 推断 digest 已核验。
5. 点击 **升级预览**：重新读取当前容器及本机候选信息，查看阻塞项、迁移/回退声明、历史备份与仅供参考的两行配置。发布证据须在 15 分钟内且对应当前目录记录；目录改变或证据过期需重查。
6. “技术项可供审查”只是镜像与登记证据满足检查，不代表允许直接升级。升级前仍须安排维护、生成并校验实例备份、明确迁移与回退、单独验收业务，再按授权维护部署配置。

网络恢复和正常状态轮询不会重发这些检查或触发升级。重新读取目录会清除旧的本机/运行预览，避免把新配置与旧核验混合。正式运行更新、数据库迁移和回退不属于当前只读页面操作。

## 维护候选目录

默认跟踪文件是 `apps/powerx/versions.json`，Admin 只读挂载到 `/config/powerx-versions.json`。不想把本地目录内容提交时，可使用已忽略的 `apps/powerx/versions.local.json`，并按授权配置根 `.env`：

```dotenv
POWERX_VERSION_CATALOG=./apps/powerx/versions.local.json
```

必须先准备有效文件；不要让 Docker 为缺失文件创建目录。目录文件格式错误会保留错误并拒绝候选，不降级成“最新版本”。公开跟踪文件的维护按本地验证、授权提交/推送和服务器同步流程执行；UI 没有上传或编辑目录接口。

`apps/powerx/versions.json.example` 提供字段骨架，**全部占位值都须替换后才能登记**。没有真实发布制品/证据时保持默认空目录，不把示例当作可部署版本。

| 目录字段 | 填写要求 |
| --- | --- |
| `schema` | 当前 XDocker 格式为整数 `1` |
| `versions[].id` | 唯一的小写字母、数字、连字符标识，最多 80 字符 |
| `channel` / `version` | `dev` 或 `stable`；正式须为不含 prerelease 的最终 SemVer tag，允许可选 `v` 前缀 |
| `source_revision` | 同一发布的完整 40 位源码 SHA |
| `images.backend` / `images.web-admin` | 对应两个 PowerX 包的 `ghcr.io/artisancloud/...@sha256:<64位digest>`；不能互换角色或填写可变 tag |
| `evidence.run_id` | 该源码的真实 PowerX 发布 workflow run ID，正整数；不是浏览器的随机 job ID |
| `evidence.release_tag` | 开发填 `null`；正式必须与 `version` 相同 |
| `migration.status` | `none`、`required`、`unknown`；由 PowerX 维护者确认 |
| `migration.from_revisions` | 本说明适用的当前源码 SHA 清单；没有当前版本的说明会阻塞技术预览 |
| `migration.notes` | 升级与回退说明，最多 2000 字符；不要填写凭据 |
| `migration.rollback` | `compatible`、`restore-required`、`unknown`；未知时阻塞，不只凭旧镜像可拉取承诺能回滚 |

当前证据适配器支持 `.github/workflows/publish-container.yml` 中同一成功任务里的 `Build backend`、`Build web admin`、`Fresh database, login, frontend and API smoke test` 三个成功完成步骤。完整 workflow 也必须 success；步骤缺失、跳过或发布契约变化保持未确认。正式还要求 `powerx-docker.tar.gz` 与其 `.sha256` 资产已经上传且非空。

这个规则是受支持发布流程的核对范围，不替代对步骤内容、发布包 bytes 或迁移脚本的项目验收。新格式/不同发布工作流由对应项目确认后再更新 XDocker 适配器，不去修改 PowerX 来迎合页面。

## 升级与回退边界

预览只输出 `POWERX_BACKEND_IMAGE` 和 `POWERX_WEB_IMAGE` 两行参考，不改数据库/Redis 镜像、安装状态、管理员、域名或数据目录。数据库迁移标为 required 时先明确影响、实施和验收方案；rollback 标为 restore-required 时须具备对应备份恢复方案。

“当前两容器与候选镜像 ID 匹配且运行中”只说明容器事实，不代表 Setup、健康接口、插件或业务验收已通过。只有停止的镜像、仅有配置或未知 Docker 状态都不判作正常已部署。

当前新增的是只读页面、证据子进程和一个只读目录文件挂载，不新增业务数据库表。回退本管理功能可回到先前已验证的 Admin 镜像；不因此执行 PowerX 镜像回退或数据库恢复。

本地用临时目录、模拟 Docker/GitHub 响应和网页事件验证；尚未对真实新 Release、私有 registry、浏览器或目标服务器执行本功能验收，也未提交、推送或部署。

参考：[Docker image inspect](https://docs.docker.com/reference/cli/docker/image/inspect/)、[GitHub workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs)、[workflow jobs API](https://docs.github.com/en/rest/actions/workflow-jobs)、[实例备份](backups.md)、[PowerX 部署与升级](README.md)。
