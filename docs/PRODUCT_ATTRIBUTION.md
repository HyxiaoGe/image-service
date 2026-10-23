# 产品身份与生图用量归因

image-service 按调用方的产品服务密钥选择方舟 Key，按产品隔离生成缓存和去重锁，并持久化调用与上游尝试记录。默认仍是兼容模式；部署这份代码不等于 Fusion 已完成接入，也不会自动创建方舟 Key。

## 产品身份

每个产品用自己的服务密钥调用 image-service，密钥决定产品身份和该产品的方舟 Key。服务密钥只存放在 image-service 和对应产品后端的服务端环境变量里，前端和模型都接触不到。

```http
Authorization: Bearer <该产品的服务密钥>
X-Image-User: <产品后端已验证登录的用户 ID>
X-Image-Scene: chat_image
X-Request-ID: <产品后端生成的调用关联 ID>
```

- `X-Image-User` 由产品后端在完成自身登录校验后填写，1–128 个字母、数字或 `_.:@-`，只用于该产品内的用户统计；不合法或省略时记为产品自身的服务调用。image-service 不再验证用户登录态，信任持有服务密钥的产品后端。
- `X-Image-Scene` 允许 `chat_image`、`image_edit`、`validation`，其他值记为 `unspecified`。场景只用于统计：Fusion 即使填 `validation`，产品归属仍是 `fusion`。
- `X-Request-ID` 为 1–128 个字母、数字或 `_.:-`，仅用于关联，不能当作幂等保证。
- 身份放在传输层，不增加模型可见的 `app_id/user_id` 工具参数；请求体或其他请求头里的 `app_id` 不影响产品归属。

## 认证模式

服务密钥在所有模式下都优先识别；`AUTH_MODE` 只决定没有带产品密钥的请求如何处理。

| `AUTH_MODE` | 未带产品密钥的请求 |
|---|---|
| `legacy`（默认） | 保留既有行为：`POST /v1/generate` 按可选 `API_KEY` 鉴权，MCP 不要求凭证。统一标记为命名的旧调用来源，不能据此认定为 Fusion |
| `mixed` | 只接受配置的旧 `API_KEY`；匿名旧调用仅在显式 `ALLOW_LEGACY_ANONYMOUS=true` 时接受 |
| `required` | 全部拒绝（HTTP 401），不接受旧 Key 或匿名调用 |

过渡请求统一归属 `LEGACY_APP_ID`（默认 `legacy`），主体类型也是 `legacy`。不要在没有证据时将其配置成 Fusion 并声称来源已被验证。

`GET /v1/usage` 在所有模式下都只接受产品服务密钥，且只能查该密钥所属产品。`/v1/health`、`/metrics`、OpenAPI 和现有静态图片 URL 的公开行为保持原样；本次接入不提供图片访问权限隔离。鉴权拒绝发生在 HTTP 边界、生图上游调用之前；HTTP 外的内存/stdio 调用在 `mixed`/`required` 模式下不允许绕过身份校验。

## 配置

在方舟创建产品专用 Key，并为每个产品生成独立的服务密钥（如 `openssl rand -hex 32`），配置在 image-service 宿主机 `.env`：

```dotenv
AUTH_MODE=legacy
PRODUCTS={"fusion":{"service_key_env":"FUSION_SERVICE_KEY","ark_key_env":"FUSION_ARK_API_KEY","ark_key_alias":"fusion","ark_resource_id":"<非秘密的Key资源ID>"},"validation":{"service_key_env":"VALIDATION_SERVICE_KEY","ark_key_env":"VALIDATION_ARK_API_KEY","ark_key_alias":"validation"}}
FUSION_SERVICE_KEY=<Fusion服务密钥>
FUSION_ARK_API_KEY=<Fusion专用方舟Key>
VALIDATION_SERVICE_KEY=<验证服务密钥>
VALIDATION_ARK_API_KEY=<验证专用方舟Key>
```

示例中的 Key 和资源 ID 都是占位。各产品不能共用同一个服务密钥变量；未配置服务密钥的产品无法被匹配。缺少专用方舟 Key 时该产品的 Seedream 调用明确失败（`config_missing`），不借用其他产品或默认的 `ARK_API_KEY`。

模型路由仍遵循 `model > quality > DEFAULT_MODEL`。普通业务产品不会参与模型决策。Gemini/DashScope 备用链目前仍使用各自已有的共享凭证，尝试记录标为 `shared-litellm` / `shared-dashscope`；不能将它们的费用称为方舟分账费用。

## 持久化与查询

`USAGE_ENABLED=true` 默认开启；默认数据库为 `storage/usage.sqlite3`，位于已有 Docker 持久卷内。可通过 `USAGE_DB_PATH` 指定。SQLite 使用 WAL，启动检查与健康检查覆盖该存储；写入等待和清理有界，避免故障无限拖长生成预算。

- 请求开始和每次提交生图上游前先写持久记录，写入失败返回 `usage_unavailable`，不提交新的上游请求。
- 记录更新失败不会重发生图，已写入的悬而未决尝试保留为待对账。取消/读写超时不当作确定免费。
- 至少保留 `USAGE_RETENTION_DAYS=90` 天；首次访问和之后每天清理更早的记录，不重建已清理的历史。
- 产品调用使用独立缓存/锁命名空间，与旧调用和其他产品隔离；同产品内相同参数可复用，场景变化不强制重新生成。
- 缓存命中算一次调用、零次上游尝试和零次新增出图；记录本次调用者与原产物归属。
- 记录字段采用白名单，不保存提示词、请求/响应原文、服务密钥、方舟 Key 或原参考图内容。

后端管理查询：

```http
GET /v1/usage?since=2026-10-23T00:00:00%2B08:00&until=2026-10-24T00:00:00%2B08:00
Authorization: Bearer <该产品的服务密钥>
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

当前可以先部署 `AUTH_MODE=legacy` 收集明确标为 legacy 的用量，不要求 Fusion 同时升级。正式分账需要各产品带上服务密钥并配好独立方舟 Key；所有合法调用方都改用产品密钥后再切 `required`。旧图片和过去不完整的日志不回填虚构产品身份。历史数据是否属于 Fusion，仍保持原来的证据边界。
