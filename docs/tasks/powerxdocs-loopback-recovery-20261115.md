# PowerXDocs 主容器 loopback 恢复

本会话用户在了解“同镜像重建主容器、保留旧实例、修改网关目标”的具体方案后明确要求执行。本次跨项目授权仅涉及 `powerxdocs-local` 的运行部署和 XDocker 接入配置，不包括 Docs 源码编辑、文档发布、镜像重建或 `powerxdocs-merged-preview`。

## 原因与目标

现有主容器的 `80/tcp` 同时发布到 `192.168.1.23:8080` 和 `100.100.0.3:8080`。前者已不是本机地址，后者在开机时延迟出现，Docker 创建网络失败。目标是 `127.0.0.1:8080 → 80/tcp`，通过 `docs.home.test` 网关访问。镜像保持原不可变 ID；保留原启动参数、资源限制、环境和自定义 Nginx 配置。

## 已审查的分阶段操作

使用本地实现、验证并推送的固定 XDocker 提交。服务器同步准确 SHA 后运行：

```sh
.venv/bin/python -B scripts/repair-powerxdocs-loopback.py prepare \
  --state /data/workspace/XDocker/data/native-admin \
  --backup /data/workspace/XDocker/data/native-admin/backups/powerxdocs-loopback-20261115 \
  --expected-container-id 231727d6afc6784cf221d6913e128fca948fb5b4b4872e9b2bc06e4fea078c06 \
  --expected-image-id sha256:db7d33dea65cce13b25f7f16debb7c7fca52d3558b66f324adb6bc52644619f0
```

`prepare` 只接受上述正式主容器、已停止状态、无持久卷、普通 bridge 网络和固定镜像。若文档目录存在容器可写层改动则阻断，不能用旧镜像覆盖这些内容。备份原始 inspect（可能含环境变量）、Nginx 配置、registry、策略和项目快照到 0700 目录，敏感文件 0600。不会输出环境或提交私有备份。

创建带独立名称的候选实例，暂不启用自动重启。复制并核对原 Nginx 配置，再启动和验证本机 HTTP；旧容器保持原名、原状态。每个阶段记录在私有 `plan.json`，失败时先核对 checkpoint，不盲目重跑创建。

确认首页和静态资源均通过、镜像 ID 和配置摘要保持一致后，执行：

```sh
.venv/bin/python -B scripts/repair-powerxdocs-loopback.py activate \
  --state /data/workspace/XDocker/data/native-admin \
  --backup /data/workspace/XDocker/data/native-admin/backups/powerxdocs-loopback-20261115
```

`activate` 再次核对原实例、候选实例及 registry 快照。旧容器改名保留，并关闭其自动重启以免继续使用失效端口绑定；新实例取得原名称，沿用 `unless-stopped`。仅将 `powerx-docs` endpoint 改为 `127.0.0.1`，通过策略 revision 校验写入。此时 checkpoint 为 `gateway-apply-required`，不会自动发布网关。

随后通过已认证的现有 Admin API 预览并应用网关：先确认所有项目仍等于备份快照、策略 SHA 等于 checkpoint，再以确切预览指纹调用 apply。配置并发变化时停止，不发布其他人的未应用修改。等待采集器发现新容器后，以 revision 校验仅更新 PowerXDocs 监控绑定，保留其他组件和其他应用。

## 验收与回退

核对本机首页、真实静态资源、`docs.home.test`、其余既有域名、镜像 ID、Nginx 配置和新容器的 `unless-stopped`；仅重启新的 Docs 容器验证可重新启动，不重启 Docker daemon 或整机。核对 preview 容器 ID/状态未变，其他项目及挂载策略保持原状。

原实例、原镜像、原配置和 registry 备份都保留。若切换失败，依据 checkpoint 停止并改名候选，恢复旧实例名称及原 restart policy；网关回退仅对 Docs endpoint 做有 revision 的反向修改并再次预览应用，不整库覆盖可能存在的并发修改。旧实例含失效地址，因此回退只恢复变更前状态，不承诺原失效入口可用。镜像及容器不自动删除。
