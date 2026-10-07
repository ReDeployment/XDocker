# 独立证书清单、检查、续期与 HTTPS 验收

新服务器的主流程是本指南第 2 节的 Certbot 容器；首次部署先阅读 [服务器准备指南](../server-setup/README.md)。第 5、6 节仅用于需要维护旧宿主机证书的情况。

## 1. 清单与网站部署独立

证书清单为 `certbot/certificates.json`，已按你提供的 `certbot certificates` 输出登记 **8 份证书、10 个域名**。不在配置中保存旧有效期、序列号、证书或私钥；运行时读取真实证书。

```json
{
  "version": 1,
  "warning_days": 30,
  "critical_days": 7,
  "acme_directory": "https://acme-v02.api.letsencrypt.org/directory",
  "certificates": [
    {
      "name": "debug-ecommerce.artisan-cloud.com",
      "domains": [
        "debug-ecommerce.artisan-cloud.com",
        "debug-scrm.artisan-cloud.com",
        "debug.artisan-cloud.com"
      ],
      "enabled": true
    }
  ]
}
```

`name` 必须与 Certbot 的 **Certificate Name** 一致，不一定等于你正在访问的域名。一个 SAN 证书始终作为一组管理，不会把三个域名拆散或缩减为一个。新增域名或变更分组先核实实际证书，再修改清单；不匹配时工具会拒绝续期，不会静默修改 SAN。

默认清单启用全部 8 份。每台服务器只保留实际承担的证书项为 `enabled: true`。可复制为 `certbot/certificates.local.json` 并修改，根目录 `.env` 设置 `CERTIFICATES_INVENTORY=./certbot/certificates.local.json`；该本地文件已被 Git 忽略。证书管理不依赖 `ENABLED_SITES`，也不要求网站镜像已经发布。

按 2026-10-06 提供的快照，优先级为：Shopify 已过期 → powerx-dev 约 5 天 → artisan-cloud、PowerWechat、PowerX 约 13 天。脚本输出会按运行时真实日期重新计算，快照不是当前状态的保证。

## 2. 新服务器：在 XDocker 容器中管理证书

先按 [服务器准备指南](../server-setup/README.md) 完成账号、SSH、Docker 与 XDocker 初始化。第一步确认容器能启动、脚本能读取清单以及容器能连接 ACME：

```bash
cd ~/workspace/XDocker
sudo docker compose -f compose.yml run --rm --no-deps certbot --version
sudo sh scripts/certificates.sh list
sudo sh scripts/certificates.sh preflight
```

Docker Hub 拉取超时先按 [运行镜像副本指南](../runtime-images/README.md) 切换现有 .env。分别应输出 Certbot 版本、REGISTERED 清单和 REACHABLE。新机 data/letsencrypt 尚为空，先运行 check 会报告证书缺失；待签发后再检查有效期。新服务器无需经过后面的旧机兼容章节，也不用复制旧证书。

已有证书签发成功后，下面是日常维护命令示例，不是新机首次执行顺序。容器入口只使用根目录公共 Compose：

```bash
sudo sh scripts/certificates.sh list
sudo sh scripts/certificates.sh check
sudo sh scripts/certificates.sh preflight
sudo sh scripts/certificates.sh renew-test powerx-doc.artisan-cloud.com
sudo sh scripts/certificates.sh renew powerx-doc.artisan-cloud.com
sudo sh scripts/certificates.sh verify powerx-doc.artisan-cloud.com --match-local
```

默认目录为 `data/letsencrypt`，通过 `.env` 的 `LETSENCRYPT_DIR` 配置绑定到容器 `/etc/letsencrypt`。如仅检查旧目录，可设置 `LETSENCRYPT_DIR=/etc/letsencrypt`；检查不改证书。

**旧 renewal 配置如果使用 nginx/apache 插件、standalone 或其他插件，不能直接放进默认 Certbot 容器并当成 webroot 续期。** 工具会拒绝这种隐式切换。旧目录继续用 `--host` 管理；迁移到新容器入口时使用独立的 `data/letsencrypt`，按下面的步骤重新签发完整域名组。若有更复杂的 DNS 插件，需要单独设计相应镜像和验证配置，当前 stock 容器只接管 webroot。

### 2.1 新服务器只准备证书验证入口

先配置公共 `.env` 的真实 `CERTBOT_EMAIL`、80/443 端口，以及本机负责的证书清单。DNS A/AAAA 需指向新服务器，公网 80 可达；所有 SAN 域名都需要能通过验证。

```bash
sudo sh scripts/certificates.sh acme-http
```

这个命令只启动公共 Nginx，并生成 `data/nginx/10-certificates-acme.conf`，为清单中尚未有网站配置的域名提供 HTTP-01 路径。普通页面返回 404，不会虚构业务服务或启动 PowerWechatDocs 等镜像。已有网站域名从这个配置中排除，网站上线时也会更新该排除清单，避免同域名重复入口。

检查挑战路径：

```bash
mkdir -p data/acme/.well-known/acme-challenge
printf 'xdocker-acme-ok\n' > data/acme/.well-known/acme-challenge/probe
curl http://shopify.artisan-cloud.com/.well-known/acme-challenge/probe
```

从其他网络也检查，应返回同一文本。成功后删除 probe。若域名仍指向旧服务器，这个新入口不会自动替旧服务器完成验证。

### 2.2 签发一个完整域名组

```bash
sudo sh scripts/certificates.sh issue-test debug-ecommerce.artisan-cloud.com
sudo sh scripts/certificates.sh issue debug-ecommerce.artisan-cloud.com
```

第一步 dry-run，第二步正式签发并同意 Let's Encrypt 服务条款。脚本按清单传入全部三个 `-d` 域名及相同 Certificate Name，保证不拆组。新签发与旧机续期是不同操作；存在另一种 renewal 方法的同名旧证书时会拒绝覆盖其方法。

成功后的证书必须被实际提供 HTTPS 的入口使用。证书工具不会自动迁移 Shopify、PowerX API 等业务后端。若网站使用 SAN 组，本站 `.env` 可设置：

```dotenv
SITE_DOMAIN=debug-scrm.artisan-cloud.com
SITE_CERT_NAME=debug-ecommerce.artisan-cloud.com
```

`SITE_CERT_NAME` 默认等于本站域名；HTTPS 模板将读取指定证书组的目录，签发也始终使用该组完整域名清单。新站点的其他字段仍按多站点指南配置。

### 2.3 自动续期与报告

业务入口/证书引用配置完成后，启动公共续期服务：

```bash
sudo docker compose -f compose.yml --profile tls up -d certbot-renew
sudo docker compose -f compose.yml logs --tail=100 certbot-renew nginx
```

每 12 小时检查清单并尝试正常续期。只在真实证书变化后原子更新 reload 事件；入口 Nginx 检测事件后检查配置并 reload。没有变化或 dry-run 不触发 reload。

持久化报告为：

```text
data/events/certificates-check.json
data/events/certificates-renew.json
```

它们包含检查时间、状态、有效期、错误和公开叶证书 fingerprint，不包含私钥。续期失败、目录缺失、SAN 不符和临近过期都输出到日志并返回非零。**目前是本地报告和日志，没有外部邮件/消息告警投递，也不能在网络断开时保证续期成功。**

仅迁移部分域名时，使用本机的 `.local.json` 禁用还在旧机负责的项；否则监控会正确报告本机缺少这些证书。

## 3. 状态和返回码

| 状态 | 意义 |
| --- | --- |
| `LOCAL_VALID` | 本地叶证书有效，SAN/私钥/续期配置匹配；公网尚需 verify |
| `EXPIRING` / `TLS_EXPIRING` | 距到期不超过 30 天，注意续期；不等于 Certbot 当前一定会签发 |
| `CRITICAL` / `TLS_CRITICAL` | 距到期不超过 7 天 |
| `EXPIRED`、`ERROR`、`TLS_FAILED` | 过期、缺失、不匹配，或者公网 TLS 失败 |
| `BLOCKED_NETWORK` | ACME API 不可达，未调用 Certbot |
| `REFUSED` | 存在不安全的 SAN/插件不匹配，未修改原配置 |
| `DRY_RUN_OK` | 实际 Certbot dry-run 成功，正式证书未更新 |
| `UNCHANGED` | Certbot 返回成功但叶证书未变化，不称为“续期完成” |
| `RENEWED` / `ISSUED` | 叶证书实际更新或首次生成；还需 reload 和公网验证 |

返回码：0 正常，1 失败/过期/严重临期，2 临期预警。`check --require-usable` 仅供 HTTPS 启用前验证“当前还可用”，允许临期证书；不要用于到期监控。检查与续期都可加 `--json --report <路径>`；报告路径放在 `data/` 或服务器日志目录，避免提交 Git。

备份整个 `/etc/letsencrypt` 或 `data/letsencrypt`，保留 `live` 符号链接、`archive`、`renewal`、账户和文件权限，不要只备份 fullchain.pem。公网启用和 reload 前必须验证实际入口；仓库脚本测试不能替代服务器验收。

## 4. 开发验证与官方依据

```bash
python3 -m unittest discover -s tests -v
```

41 项测试通过，覆盖有效期、过期、私钥/SAN 不匹配、网络失败时零续期操作、旧 nginx 插件保护、SAN 组完整签发、真正更新才 reload、Nginx 检查失败、TLS/SNI 验证逻辑和本地/对端叶证书不一致。开发测试用 cryptography 生成临时证书和本地 TLS 服务；不会访问 ACME 或更改生产服务器。

另外，临时宿主机 Nginx 验证了清单中全部 10 个域名的 ACME 路径返回 200，未部署业务的路径返回 404。开发机 Docker daemon 尚未启动；新服务器已回传 Docker/Compose 运行及 init 成功，但 Certbot 容器、旧机证书读取/续期、正式签发和公网 HTTPS 验证均未完成，不能据此称原证书已恢复。

保留旧 renewal 选项、dry-run、`--cert-name` 及 hook 的语义参见 [Certbot 官方用户指南](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)。如另行改变旧验证方式，官方建议用 reconfigure 并先验证成功，而不是手改 renewal 文件；当前工具没有自动重写这些文件。

## 5. 可选：旧 Ubuntu 服务器检查

旧机已有宿主机 Certbot，先使用兼容入口，它不需要启动 Docker 或重新安装 Certbot：

```bash
cd /home/ubuntu/workspace/XDocker
sudo sh scripts/certificates.sh --host list
sudo sh scripts/certificates.sh --host check
```

默认读取 `/etc/letsencrypt`。只检查某一份：

```bash
sudo sh scripts/certificates.sh --host check shopify.artisan-cloud.com
```

主机需要 Python 3.8+；读取证书优先使用 cryptography，未安装该 Python 模块时自动使用 OpenSSL，不必为了检查额外安装 Python 库。实际续期使用现有的 `certbot` 命令；需要时通过 `--certbot-bin /snap/bin/certbot` 指定。

服务器若无法拉取 GitHub，可以从本地通过 SCP 保持目录结构复制 `scripts/certificates.sh`、`scripts/certificates.py` 和 `certbot/certificates.json`，即可运行旧机检查和续期，不需要复制私钥到本地或先部署其他服务。

检查项：真实有效期、全部 SAN、私钥匹配、续期配置是否可读、原 authenticator/installer。**本地文件正常不等于公网 HTTPS 正常**；后面还有独立的公网检查。

## 6. 可选：旧机连通性与续期

优先修 Shopify，命令如下：

```bash
sudo sh scripts/certificates.sh --host preflight shopify.artisan-cloud.com
sudo sh scripts/certificates.sh --host renew-test shopify.artisan-cloud.com
sudo sh scripts/certificates.sh --host renew shopify.artisan-cloud.com --reload host-nginx
sudo sh scripts/certificates.sh --host verify shopify.artisan-cloud.com --match-local
```

- `preflight` 检查原续期配置中的 ACME API（没有该配置时使用清单默认 URL），不申请证书。
- `renew-test` 先检查生产和 Let’s Encrypt staging API，再执行该证书的真实续期 dry-run。原插件可能临时修改/reload Nginx 来验证域名；不保存测试证书。
- `renew` 保留原 renewal 配置和插件，用 `certbot renew --cert-name ...`；不传 `--force-renewal`，不偷偷换成 webroot。只有实际叶证书变化才记录 `RENEWED`。
- `--reload host-nginx` 仅在实际更新后执行 `nginx -t` 与 `systemctl reload nginx`；检查失败会报告“文件已更新但 reload 失败”，不报告完成。
- `verify` 从公网域名的 443 检查 CA 信任链、主机名、有效期和 SNI。`--match-local` 还确认 Nginx 正在提供本地这张叶证书；前面有 CDN/外部 TLS 终止时不要使用该选项。

如果 preflight 返回 `BLOCKED_NETWORK` / `UNREACHABLE`，先修复 ACME 出站访问或迁移到可访问外网的服务器。工具不会继续连续尝试全部证书。镜像能下载不代表 ACME API 能访问，ACME API 能访问也不代表域名验证已通过。

单份成功后处理 powerx-dev，再根据真实检查结果处理其他证书。批量命令省略 Certificate Name：

```bash
sudo sh scripts/certificates.sh --host renew-test
sudo sh scripts/certificates.sh --host renew --reload host-nginx
sudo sh scripts/certificates.sh --host verify --match-local
```

批量只操作已启用项，某项失败会记入报告并继续其他项；返回码仍表示整批有问题。不要同时运行多个写入同一个证书目录的 Certbot 定时任务。
