# SSH 隧道网页管理

状态：2026-10-14，本地实现；本机后台安装、管理镜像发布及服务器更新分别按用户授权执行。

本机实现位于 `clients/ssh-tunnel/`，独立于服务器容器服务。

控制台「网站与 HTTPS → SSH 隧道」（`/#ssh-tunnel`）与 FRP 穿透分开。本机发起 SSH 本地端口转发，例如本机 `127.0.0.1:19080` → 服务器 `127.0.0.1:9080`。远程控制台内展示本机管理页，本机服务执行连接、断开及重连；无需在服务器安装 SSH 私钥或开放新的公网管理端口。

## 在本机启动

以下命令在本机 XDocker 仓库运行，不在服务器运行，不需要本机 Docker或 sudo：

```sh
sh scripts/tunnel-manager.sh init
sh scripts/tunnel-manager.sh run
```

保持终端运行，打开 `http://127.0.0.1:19079/`。在自己的另一个终端读取令牌并填入本机页面：

```sh
sh scripts/tunnel-manager.sh token
```

这是独立的本机管理令牌，与服务器 Admin 令牌、GHCR PAT、FRP token 分开。仅保存在本页内存，不发送至远程控制台、不存入浏览器本地存储或 URL；私有文件 `data/tunnel-manager/access-token` 为 600，整个目录 700。SSH 认证继续使用已有系统 SSH 配置、SSH agent 和严格主机身份验证。

macOS 在批准后台安装后，可停止前台 `run`，再执行：

```sh
sh scripts/tunnel-manager.sh start
sh scripts/tunnel-manager.sh status
sh scripts/tunnel-manager.sh stop
```

本机管理器使用独立 LaunchAgent；停止管理器不停止已有 SSH 隧道。Linux 支持本机页面和读取状态，网页启停当前标为不支持；Linux 继续使用 `admin-tunnel.sh run`。

## 页面操作

1. 进入控制台的 SSH 隧道页，或打开独立本机管理页，输入本机令牌。已有本仓库连接按本机端口列出。
2. 新连接填写现有 SSH 别名、本机端口及服务器端口，确认完整 SSH 目标后连接。不能接管已占用端口或另一配置；先在终端确认该别名的密钥登录与 known_hosts 登记。
3. 分别核对 SSH 监督状态、任务登记和管理 HTTP 状态。登记成功不等于已连接；认证、主机身份和端口错误不能自动当成成功。
4. 断开/重连仅作用于所选连接及当前仓库。前台 `run`、其他 SSH、VS Code 和 FRPC 不按 PID 强行停止。当前控制台连接断开时远程页面会失联，独立本机管理页仍可用。
5. 请求失败后先读取实际状态和本机操作记录，再决定是否重新提交。后台 GET 轮询和网络恢复不重发启停请求。

本机服务固定绑定回环地址，并校验 Host、受允许的本机 Origin、独立令牌、操作字段、配置版本和目标确认。网页只调用限定的隧道生命周期函数，不提供任意 shell、远程命令或私钥上传。状态/配置位于 `data/admin-tunnel/<本机端口>/`；网页操作记录位于 `data/tunnel-manager/jobs.json`，最多保留最近 50 项。未知或不安全状态文件保留原文件并拒绝管理。

控制台嵌入默认本机 `19079` 服务；跨来源 iframe 隔离本机令牌。只有健康 GET 请求由远程控制台页面发往此固定地址。管理员可在本机 CLI 使用 `--port` 选择独立管理页端口，控制台嵌入仍使用默认 19079。未启动服务时控制台显示初始化说明。

## 验证与回退

本地验证应使用独立 SSH 转发端口，覆盖连接、实际 HTTP、重连、断开、端口冲突、请求中断和已有连接保留；未安装/未发布的部分应分别记录。源码与页面验证不代表本机自启安装或服务器镜像已更新。

回退界面使用此前管理镜像；回退本机管理器执行 `tunnel-manager.sh stop`，已有隧道可继续用 `admin-tunnel.sh status/start/stop` 管理。删除私有记录前先停止对应管理器；不要删除正在运行隧道的监督锁文件。
