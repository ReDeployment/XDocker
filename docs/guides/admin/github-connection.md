# GitHub App 只读连接与自动更新

适用版本：包含 GitHub App 认证、自动续取、首次补历史与事件刷新的新版 Admin。部署时使用经过 CI 验收的固定镜像版本；仅刷新旧版网页不会增加这些能力。

日常使用入口是「研发协作 → 研发事件 → 待处理问题」。管理员只需配置一次 App，之后不必逐个采集，也不必每月手动更换个人令牌。系统读取 GitHub API 与既有 Webhook，不读取邮箱。

## 为什么使用 GitHub App

我们需要持续读取六个仓库的 PR、机器人评论、Actions、Checks 与 Release。GitHub 的 PAT 创建说明将 Checks API 列为细粒度令牌的限制，虽然部分端点文档声称支持；实际 PAT 页面可能没有 Checks 权限。不要用扩大 PAT 权限或让用户寻找不存在的选项来代替完整接入。

GitHub App 支持独立的 Checks 读取权限。应用私钥签署 RS256 JWT，后台以它换取安装令牌；安装令牌有效期约一小时，XDocker 在到期前五分钟自动续取。用户不需要长期保存访问令牌。见 [PAT 限制](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens-limitations)、[App JWT](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)、[安装令牌](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)。

## 1. 创建应用

GitHub → Settings → Developer settings → GitHub Apps → New GitHub App：

| 字段 | 填写方式 |
| --- | --- |
| App name | 自己的唯一应用名称，例如 `My-XDocker-Watch` |
| Description | GitHub 项目只读监控 |
| Homepage URL | XDocker 的公开仓库地址即可 |
| Callback／Redirect URL | 留空 |
| Request user authorization during installation | 不勾选 |
| Device Flow | 不勾选 |
| Setup URL | 留空 |
| Webhook → Active | 取消勾选，沿用既有仓库 Webhook |
| Where can this App be installed? | 跨所属方时选 Any account |

Any account 允许安装到两个所属方，不会把私有仓库公开。安装时继续选 Only select repositories。组织可能要求管理员批准安装或新权限；只有批准后才能作为授权就绪。创建字段与安装流程见 [GitHub 创建说明](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)、[安装说明](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app)。

Repository permissions 全部选 Read-only：Actions、Checks、Commit statuses、Contents、Issues、Pull requests、Metadata。其余 No access；不需要客户端 secret、个人 OAuth、Administration、Packages 或 Webhooks 管理权限。

## 2. 分别安装到两个所属方

应用左侧 Install App，分别安装，并选择固定仓库：

| 所属方 | Only select repositories |
| --- | --- |
| ArtisanCloud | PowerX、PowerXPlugin、PowerXPluginSCRM、PowerXPluginECommerce |
| Matrix-X | PowerXPluginMediaXStudio、PowerXPluginCRM |

记录应用设置页的 **App ID**（不是 Client ID），以及两个 Configure 页面地址中 `/installations/` 后面的 **Installation ID**。这三个数字不是访问凭据；仍只放入本机／服务器运行配置，不写入公共模板。

## 3. 生成与保管私钥

应用设置 → Credentials → Key pairs → New key；部分界面为 Private keys → Generate a private key。下载 `.pem` 文件，保存到密码管理器的文档／附件作为备份。不要把 PEM 正文发到聊天、提交 Git 或上传进镜像。见 [GitHub 私钥说明](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)。

运行环境需要一份私有文件；1Password 的备份不会自动注入 XDocker。使用下面的初始化命令复制私钥并生成配置，下载文件可保持原样：

```sh
python3 scripts/github-app.py init \
  --state data/admin \
  --app-id YOUR_APP_ID \
  --installation artisancloud=ARTISANCLOUD_INSTALLATION_ID \
  --installation matrix-x=MATRIX_X_INSTALLATION_ID \
  --pem /你的私钥文件路径/app.private-key.pem
```

将大写占位符替换为自己的数字。命令仅操作当前机器的私有文件，不访问网络、不重启服务。第一次不允许覆盖已有 App 配置；有意轮换配置时显式增加 `--replace`。应由 Admin 的运行用户执行，服务器部署用户的 `id -u` 应与 `services/admin/.env` 的 `ADMIN_UID` 一致。

生成位置：

```text
data/admin/github/credentials/app.json
data/admin/github/credentials/app-keys/<私钥文件SHA256>.pem
```

目录权限 700，配置与 PEM 权限 600。不接受凭据目录／文件符号链接、公开可读文件、无效配置或不匹配的 key_id。先写入新私钥，再原子替换指向它的配置，避免轮换时读取到不同代的文件；旧密钥文件保留供受控回退，确认新配置正常后再清理不用的副本。

配置同时包含 App ID、两个所属方的安装 ID 和 key_id；由脚本生成，不手动填写固定服务器地址，不把真实环境值放进 example。App 配置存在时优先使用它，配置错误不静默退回旧个人令牌。

## 4. 验证实际权限

在安装了 `services/admin/requirements.txt` 的 Python 环境运行：

```sh
python3 scripts/github-app.py verify --state data/admin
```

它先核对安装所属方、App ID、暂停状态与权限；只向固定安装令牌端点发起认证 POST，随后对六个固定仓库发送只读 GET，验证分支、Checks、Actions、commit statuses、评论、PR 和 Release。不会修改 GitHub 仓库、重跑 CI、创建回调或发布镜像。仅输出仓库与验证状态，不输出令牌、JWT、PEM、评论或原始响应。

默认六条 `verified` 才表示这次真实连接验收通过；排查单个项目时可追加 `--project PowerXPluginCRM` 等固定项目名。连接文件存在、App 已创建、安装页面能打开都不足以代表所有权限成功。403／缺少权限时检查 App 的权限与安装审批，404 时核对私有仓库选择；不能直接判断仓库不存在。网络失败应与权限失败分别处理。

可再明确执行有界历史观察：

```sh
python3 scripts/github-watch.py --state data/admin --project all --backfill
```

不加 `--gh` 才使用这里配置的 App；`--gh` 是显式使用当前机器已有 GitHub CLI 认证，只供独立验证，不代表服务器 App 已接通。本机验收建议使用 `--state data/自己的私有验收目录`，不要覆盖服务器观察库。

## 5. 部署新版并一次性开启自动更新

先完成批准的源码、Admin 与 Webhook 镜像发布流程；不在服务器直接改源码，不使用本机 Docker。备份服务器 Admin 配置、观察数据库和凭据后，同步批准的提交与镜像，再用对应部署账号初始化服务器私有 App 文件。

服务器私有 `services/admin/.env`：

```dotenv
ADMIN_GITHUB_WATCH_ENABLED=true
ADMIN_GITHUB_WATCH_INTERVAL=300
ADMIN_GITHUB_WEBHOOK_ENABLED=true
```

按已批准的部署流程重建 Admin。现有 `data/admin:/state` 私有挂载可供认证读取，无需新增公网端口或把私钥塞进环境变量。新依赖 `cryptography` 与认证模块必须进入 Admin 镜像；不能只复制前端文件或刷新浏览器。

启用后：

1. 后台立即检查凭据就绪的项目，自动补入近期历史；单次上限 100 条，剩余继续。
2. Webhook 导入后为对应项目排队刷新；15 秒合并事件，单项目两次检查至少间隔 30 秒。
3. 每 300 秒自动补漏，包括观察窗口内已关闭 PR 的审查。关闭网页不影响采集；网页每 5 秒只读取已保存结果。
4. 安装令牌只缓存在 Admin 进程内，按所属方和仓库隔离，到期前五分钟续取。重启后重新申请；配置或密钥改变使旧缓存失效。采集子进程仅从标准输入取得令牌，不进入命令参数、环境、SQLite 或日志。GitHub 返回 401 后清掉缓存，在冷却后的下次采集重新认证。
5. 限流遵守 GitHub 冷却时间，其他失败从 60 秒指数退避至一小时，保留旧记录并显示待核对。仓库读取有 45 秒预算／50 秒子进程上限；首次认证额外最多两次 8 秒请求。

## 6. 网页验收与回退

`/#github/hooks` 显示两个所属方的 GitHub App 配置、App ID 和安装 ID；不展示令牌或私钥。`/#github/projects` 应从等待连接变为正在检查，最终显示最近完成时间和结果。实际 CI 失败不要求显示绿色。

- `/#github/issues`：CI、评论和审查证据，独立人工处理状态。
- 项目「查看详情」：PR、检查和失败步骤，以及已收到的消息；尚无完整检查也说明原因并显示事件。
- `/#github/releases`：正式／开发预发布记录；存在 Release 不等于镜像验收通过。

旧的 `artisancloud.token`、`matrix-x.token` 文件读取仍兼容，但不作为新接入首选，也不承诺没有 Checks 权限的 PAT 能完成全套采集。回退 App 配置应受控停用自动更新并恢复旧配置／镜像，不覆盖仍在写入的观察数据库。回退不等于撤销 GitHub 私钥；废弃密钥由维护者在 GitHub 单独撤销。

既有仓库 Webhook 不需要改成 App Webhook。Release 接入仍需发布新版接收器并向六个既有回调追加 `release`；见 [通知管理](github-triage.md)。
