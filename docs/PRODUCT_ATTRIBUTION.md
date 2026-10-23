# 产品身份与生图用量归因

image-service 支持按已认证产品选择方舟 Key，按产品隔离生成缓存和去重锁，并持久化调用与上游尝试记录。默认仍是兼容模式；部署这份代码不等于 Fusion 已完成接入，也不会自动创建方舟 Key。

## 认证模式

| `AUTH_MODE` | REST/MCP 行为 |
|---|---|
| `legacy`（默认） | 保留既有行为：`POST /v1/generate` 按可选 `API_KEY` 鉴权，MCP 不要求凭证。统一标记为命名的旧调用来源，不能据此认定为 Fusion |
| `mixed` | REST/MCP 接受目标 JWT 或配置的旧 `API_KEY`；匿名旧调用仅在显式 `ALLOW_LEGACY_ANONYMOUS=true` 时接受 |
| `required` | REST/MCP 只接受 auth-service 为 image-service 签发的目标 JWT，不接受旧 Key 或匿名调用 |

过渡请求统一归属 `LEGACY_APP_ID`（默认 `legacy`），主体类型也是 `legacy`。不要在没有证据时将其配置成 Fusion 并声称来源已被验证。无效 Bearer 不会因为允许匿名兼容而降级放行。

`GET /v1/usage` 在所有模式下都不接受匿名或旧凭证；只允许含 `usage:read` 的目标 JWT，且只能查该 Token 所属产品。`/v1/health`、`/metrics`、OpenAPI 和现有静态图片 URL 的公开行为保持原样；本次接入不提供图片访问权限隔离。

## auth-service 待提供的 Token 契约

image-service 复用 `seanfield-auth-client==0.3.1` 的 RS256/JWKS 验签 SDK，并额外校验以下字段。现有 Fusion 登录 Token 的受众是 Fusion，不能直接作为 image-service 凭证。

```json
{
  "iss": "<现有 auth-service 的精确 issuer>",
  "aud": "image-service",
  "type": "access",
  "principal_type": "user",
  "sub": "<用户 ID>",
  "app_id": "fusion",
  "azp": "fusion-api",
  "scopes": ["image:read", "image:generate"],
  "iat": 0,
  "exp": 300,
  "jti": "<唯一 Token ID>",
  "sid": "<现有登录会话 ID>",
  "auth_generation": 0
}
```

示例中的时间是格式占位，实际必须是当前有效的 NumericDate，`0 < exp-iat <= 300`。`sub/jti/app_id/azp` 为 1–128 个字母、数字或 `_.:@-`；产品 ID 配置进一步限制为 1–64 个字母、数字或 `_.-`。用户主体必须带合法 `sid` 和非负整数认证代际；服务主体使用 `principal_type=service` 和独立的 `sub`，不要求用户会话字段。

签发端负责校验来源应用和 Fusion 后端身份、继承用户、限制目标资源和权限，且换票不延长原用户授权。此处的新增 claims、用户委托换票和服务主体签发尚需 auth-service 实现；image-service 不签发 Token，不持有 auth-service 私钥，不宣称具备完整 OAuth/OIDC/MCP OAuth 自动发现能力。

用户会话校验复用 `revoked_sid:{sid}` 和 `revoked_user:{sub}`。必须把 `AUTH_REDIS_URL` 指向 auth-service 使用的同一 Redis 数据库；为空时使用当前 `REDIS_URL`。与现有认证契约一致，Redis 撤销查询不可用时记录告警，短期退化为 JWT 自身有效期。`auth_generation` 由签发端确认，资源端不根据请求参数修改它。

读取/发现操作要求 `image:read`，生图要求 `image:generate`，管理用量要求 `usage:read`。MCP 在 HTTP 边界解析有界 JSON 请求并检查工具权限，鉴权拒绝使用 HTTP 401/403，发生在生图上游调用之前。HTTP 外的内存/stdio 调用在新认证模式下不允许绕过身份校验。

## 配置专用方舟 Key

在方舟创建产品专用 Key，再配置在 image-service 宿主机 `.env`；Fusion 不持有这些 Key。

```dotenv
AUTH_MODE=mixed
AUTH_ISSUER=https://auth.example.com
AUTH_JWKS_URL=http://127.0.0.1:8100/.well-known/jwks.json
AUTH_AUDIENCE=image-service
AUTH_REDIS_URL=redis://127.0.0.1:6379/0
PRODUCTS={"fusion":{"authorized_parties":["fusion-api"],"ark_key_env":"FUSION_ARK_API_KEY","ark_key_alias":"fusion","ark_resource_id":"<非秘密的Key资源ID>"},"validation":{"authorized_parties":["validation-cli"],"ark_key_env":"VALIDATION_ARK_API_KEY","ark_key_alias":"validation"}}
FUSION_ARK_API_KEY=<Fusion专用方舟Key>
VALIDATION_ARK_API_KEY=<验证专用方舟Key>
```

所有示例地址、客户端 ID、Key 和资源 ID 都是占位，必须替换为实际注册/配置。`AUTH_ISSUER` 匹配签名中的公网 issuer；JWKS 可以使用可信的内网地址。`azp` 必须属于该产品的 `authorized_parties`。未知产品、未授权客户端和缺少专用 Key 都不能借用其他产品或默认的方舟 Key。

模型路由仍遵循 `model > quality > DEFAULT_MODEL`。普通业务产品不会参与模型决策。Gemini/DashScope 备用链目前仍使用各自已有的共享凭证，尝试记录标为 `shared-litellm` / `shared-dashscope`；不能将它们的费用称为方舟分账费用。

## Fusion 请求方式

```http
Authorization: Bearer <image-service目标Token>
X-Image-Scene: chat_image
X-Request-ID: <Fusion后端生成的调用关联ID>
```

场景允许 `chat_image`、`image_edit`、`validation`，其他值记为 `unspecified`。场景只用于统计：Fusion 即使填 `validation`，产品归属仍为签名中的 `fusion`。`X-Request-ID` 仅用于关联，不能当作幂等保证。

身份放在传输层，不增加模型可见的 `app_id/user_id/token` 工具参数。Fusion 需按用户、会话、目标资源及权限缓存/更新 Token，不能把某个用户的 Token 存为共享 MCP 凭证。模型和普通前端不需要接触上游 Key。

## 持久化与查询

`USAGE_ENABLED=true` 默认开启；默认数据库为 `storage/usage.sqlite3`，位于已有 Docker 持久卷内。可通过 `USAGE_DB_PATH` 指定。SQLite 使用 WAL，启动检查与健康检查覆盖该存储；写入等待和清理有界，避免故障无限拖长生成预算。

- 请求开始和每次提交生图上游前先写持久记录，写入失败返回 `usage_unavailable`，不提交新的上游请求。
- 记录更新失败不会重发生图，已写入的悬而未决尝试保留为待对账。取消/读写超时不当作确定免费。
- 至少保留 `USAGE_RETENTION_DAYS=90` 天；首次访问和之后每天清理更早的记录，不重建已清理的历史。
- 产品 JWT 使用独立缓存/锁命名空间，与旧调用和其他产品隔离；同产品内相同参数可复用，场景变化不强制重新生成。
- 缓存命中算一次调用、零次上游尝试和零次新增出图；记录本次调用者与原产物归属。
- 记录字段采用白名单，不保存提示词、请求/响应原文、Token、密钥、Refresh Token 或原参考图内容。

后端管理查询：

```http
GET /v1/usage?since=2026-10-23T00:00:00%2B08:00&until=2026-10-24T00:00:00%2B08:00
Authorization: Bearer <具有usage:read的目标Token>
```

时间必须含时区，按 `[since, until)` 查询。应用来自已认证身份；查询参数不能切换到其他产品。返回调用数、落盘图片数、缓存命中、错误/取消、未完成请求、上游尝试、可能计费的尝试、未定价成功响应、按场景/模型细分以及标价估算。`estimate_complete=false` 表示有未定价响应或可能计费的未决尝试，不能把这些金额当作零。

在服务器通过受控运维权限查询，兼容模式也可使用：

```bash
docker exec image-service python -m src.usage_report --app-id legacy
docker exec image-service python -m src.usage_report --app-id fusion --since 2026-10-23T00:00:00+08:00 --until 2026-10-24T00:00:00+08:00
```

备份请使用 SQLite 的在线 Backup API，或停止写入后复制数据库；WAL 工作期间不能只复制主 `.sqlite3` 文件并假定记录完整。数据库目录必须保留在持久卷中，备份不包含方舟密钥。

## 费用口径与上线边界

方舟已成功返回图片的尝试按记录的 wire model、输出像素与参考图数量估算，价格版本为 `ark-2026-10-23`。未知模型、缺失像素、Gemini/Qwen 等未定价响应明确记为未定价；待定的超时、断线、取消也需与官方账单核对。额外参考图检索、判断模型、Fusion 聊天模型等费用不属于这份生图估算。

官方实扣按专用 Key 资源 ID 的分账账单确认：[官方分账说明](https://docs.volcengine.com/docs/ark/practice-guidance-for-modelark-bill-split?lang=zh)。服务记录不包含优惠、赠额和账单调整，不伪装成官方实扣。

当前可以先部署 `AUTH_MODE=legacy` 收集明确标为 legacy 的用量，不要求 Fusion 同时升级。正式分账需要 auth-service 签发能力、Fusion 目标身份注入和真实的独立方舟 Key 全部就绪；先在 mixed 模式联调，再切 required。旧图片和过去不完整的日志不回填虚构产品身份。历史数据是否属于 Fusion，仍保持原来的证据边界。
