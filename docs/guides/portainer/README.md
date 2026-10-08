# Portainer CE 管理面板

Portainer 是 XDocker 的可选基础服务，与 `services/frps/` 平级。使用官方公开镜像 `portainer/portainer-ce:2.45.2`，无需 GHCR PAT；其他网站的私有镜像仍保持各自权限。

```text
本机浏览器 http://127.0.0.1:19000
    → SSH 加密隧道
    → 服务器 127.0.0.1:9000
    → Portainer → Docker socket → 本服务器容器
```

端口固定绑定服务器 `127.0.0.1`，只可调整端口号。不开放公网 9000、9443、8000；不需要新增 DNS 或申请证书。HTTP 仅在本机回环和 SSH 隧道内使用。Portainer 不接入业务容器的共享网络，也不经公网 Nginx 转发。

## 启用

先在本地验证并推送仓库，服务器再 `git pull --ff-only` 并核对提交 SHA，遵守根目录 [AGENT.md](../../../AGENT.md)。以下运行配置在服务器维护，不提交 Git：

```bash
cd ~/workspace/XDocker
sh scripts/hosting.sh init
```

编辑根 `.env`，在现有 `ENABLED_SERVICES` 后追加 `portainer`。已有 FRPS 时使用 `ENABLED_SERVICES="frps portainer"`，保留现有 `ENABLED_SITES`、邮箱和其他配置。

编辑 `services/portainer/.env`：

```dotenv
PORTAINER_IMAGE=portainer/portainer-ce:2.45.2
PORTAINER_PULL_POLICY=missing
PORTAINER_PORT=9000
```

随后执行（已加入 docker 组且重新登录时可省略 sudo）：

```bash
sudo sh scripts/hosting.sh compose config --quiet
sudo sh scripts/hosting.sh compose pull portainer
sudo sh scripts/portainer.sh start
sudo sh scripts/portainer.sh status
curl -fsS http://127.0.0.1:9000/api/system/status
```

预期：容器 `Up`，API 返回 Portainer 版本。只启动面板，不重新创建网站、FRPS 或 Nginx。确认镜像已缓存后，可设 `PORTAINER_PULL_POLICY=never`；缺少该镜像时会立即失败。

## Docker Hub 无法下载

可以从能访问 Docker Hub 的可信电脑下载**相同版本和目标架构**，再通过 SSH 传输，不必开放包权限：

```bash
# 在联网电脑执行；当前服务器是 amd64，不要导出 Mac 的 arm64 镜像。
docker pull --platform linux/amd64 portainer/portainer-ce:2.45.2
docker save --platform linux/amd64 -o portainer-ce-2.45.2-amd64.tar portainer/portainer-ce:2.45.2
shasum -a 256 portainer-ce-2.45.2-amd64.tar
scp portainer-ce-2.45.2-amd64.tar ubuntu@YOUR_SERVER_IP:~/

# 在服务器执行；先与上一条 SHA256 比较。
sha256sum ~/portainer-ce-2.45.2-amd64.tar
sudo docker load -i ~/portainer-ce-2.45.2-amd64.tar
sudo docker image inspect portainer/portainer-ce:2.45.2 --format '{{.Os}}/{{.Architecture}}'
```

设 `PORTAINER_PULL_POLICY=never` 后再运行 `start`。无需修改 Docker daemon，也无需重启已运行的容器。

## 从本机打开

在自己的 Mac 终端执行，保持该终端运行：

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:19000:127.0.0.1:9000 ubuntu@YOUR_SERVER_IP
```

当前服务器可使用已有 SSH 别名 `Tianliyun-shipu` 替代 `ubuntu@YOUR_SERVER_IP`。浏览器打开 **http://127.0.0.1:19000**。若本机 19000 已占用，改左侧端口和浏览器地址；如果修改了 `PORTAINER_PORT`，改右侧 9000。

也可在 VS Code Remote SSH 的“端口”面板转发服务器 9000，保持私有访问；使用 VS Code 显示的本机转发地址。

## 首次创建管理员

管理员账号与服务器 ubuntu、GitHub PAT 均无关联。由实际管理员在浏览器中设置密码，不在 Git、`.env` 或聊天中保存。

1. 先建立隧道，打开初始化页面。
2. 在服务器自己的终端执行 `sudo sh scripts/portainer.sh setup-token`，复制输出到页面的 **Setup token** 字段；该输出是一次性秘密，不要分享日志。
3. 填写管理员用户名、密码和确认密码，点击 **Create user**。建议关闭不需要的匿名统计选项。
4. 点击 **Get Started**，打开 `local` Docker 环境，再进入 **Containers**。服务配置通过 Docker socket 自动连接本服务器。
5. 应看到 Nginx、证书续期、三个静态站、FRPS 和 Portainer；可打开容器查看 **Logs**、**Stats**、**Inspect**。

Portainer 首次初始化有约 5 分钟超时。若出现安全超时提示，在服务器执行 `sudo sh scripts/portainer.sh restart`，重新读取设置令牌，再刷新页面；不要删除 `data/portainer/`。管理员已创建后不再需要设置令牌。

官方说明：[首次设置令牌](https://docs.portainer.io/faqs/installing/setup-token)、[CLI 配置](https://docs.portainer.io/advanced/cli)、[CE 安装](https://docs.portainer.io/start/install-ce/server/docker/linux)。

## 管理边界与数据

- 面板用于查看容器、日志、资源使用和明确授权的启停。它通过 Docker socket 拥有服务器级管理能力，面板管理员必须是服务器可信管理员；把 socket 挂载为 `:ro` 也不能把 Docker API 变为只读。
- XDocker 的 Compose 由 `hosting.sh` 合并根配置、基础服务和多个站点；它不会自动变成 Portainer 可编辑的 Stack。面板看到 `external` Stack 属于正常情况。部署配置、镜像升级和路由仍在本地仓库修改、验证、推送，再同步服务器；不要复制配置在面板里另建同名 Stack。
- 面板不是 FRPC、本机进程或应用业务后台。本机原生 FRPC、尚未部署的 PowerX/Postgres/Redis 不会自动出现在这里。
- `data/portainer/` 保存面板数据库、管理员与环境配置，目录权限 700，不提交 Git。备份或升级前停面板并完整备份该目录，保留镜像版本；回滚版本应恢复对应备份，不能保证新数据库兼容旧版本。
- 不要向面板导入已有 GHCR PAT，仅查看容器无需它。Inspect/环境变量可能显示其他服务的运行秘密，不要分享截图或导出到公开仓库。

```bash
sudo sh scripts/portainer.sh status
sudo sh scripts/portainer.sh stop       # 保留数据，其余服务继续运行
sudo sh scripts/portainer.sh start
```

要从配置中取消启用，先 `stop` 再从 `ENABLED_SERVICES` 移除 `portainer`。不执行整个 XDocker 的 `down` 或 `--remove-orphans`。

## 当前验证记录

2026-10-08，`160.202.238.184` 已运行 CE 2.45.2，页面及本地 JS 资源经 SSH 隧道返回 200，版本 API 返回 2.45.2，未登录容器 API 返回 401。其余六个运行容器的 ID 与启动时间保持一致；三个静态网站和五条 FRP `/healthz` 均返回 HTTPS 200。

本地 49 项回归与 Compose 配置检查通过；本机未运行 Docker daemon，容器运行验证在同步已推送提交后的服务器完成。浏览器自动化当前不可用；首次管理员创建、登录后的容器管理操作需由管理员完成验收。
