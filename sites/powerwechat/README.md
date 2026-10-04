# PowerWechatDocs

这是独立静态站点的部署模板，默认未启用，镜像尚未在此任务中发布。

先在源码仓库发布自己的 GHCR 镜像，替换 `.env` 中的 `SITE_IMAGE` 占位版本，并确认 `SITE_DOMAIN`。再将 `powerwechat` 加入根目录 `ENABLED_SITES`，按 [多站点指南](../../docs/guides/hosting/README.md) 执行 HTTP、签发和 HTTPS 验证。
