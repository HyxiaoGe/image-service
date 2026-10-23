# 产品身份与生图用量归因

image-service 按调用方的产品服务密钥选择方舟 Key，按产品隔离生成缓存和去重锁，并持久化调用与上游尝试记录。REST 和 MCP 统一要求产品服务密钥，不再提供匿名或旧共享 Key 兼容路径。

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

## 认证与迁移

默认且仅支持严格产品密钥认证。`AUTH_MODE` 可以省略或设为 `required`；`legacy` / `mixed` 配置会启动失败，避免旧配置意外开放调用。旧 `API_KEY`、`ALLOW_LEGACY_ANONYMOUS`、`LEGACY_APP_ID`、`RATE_LIMIT_PER_IP` 和共享 `ARK_API_KEY` 已移除，不再参与认证或凭证选择。部署前应删除这些过时配置，保留产品专用 Key。

REST `/v1/generate`、`/v1/models`、`/v1/usage` 和整个 HTTP `/mcp` 端点均要求产品服务密钥，缺失、无效或误配到多个产品的相同密钥返回 401。`GET /v1/usage` 只能查该密钥所属产品。内存/stdio 生图和模型读取不提供匿名绕过。

`/v1/health`、`/metrics`、OpenAPI 和现有静态图片 URL 的公开行为保持原样；本次接入不提供图片访问权限隔离。鉴权拒绝发生在生图上游调用之前。

## 配置

在方舟创建产品专用 Key，并为每个产品生成独立的服务密钥（如 `openssl rand -hex 32`），配置在 image-service 宿主机 `.env`：

```dotenv
AUTH_MODE=required
PRODUCTS={"fusion":{"service_key_env":"FUSION_SERVICE_KEY","ark_key_env":"FUSION_ARK_API_KEY","ark_key_alias":"fusion","ark_resource_id":"<非秘密的Key资源ID>"},"validation":{"service_key_env":"VALIDATION_SERVICE_KEY","ark_key_env":"VALIDATION_ARK_API_KEY","ark_key_alias":"validation"}}
FUSION_SERVICE_KEY=<Fusion服务密钥>
FUSION_ARK_API_KEY=<Fusion专用方舟Key>
VALIDATION_SERVICE_KEY=<验证服务密钥>
VALIDATION_ARK_API_KEY=<验证专用方舟Key>
```

示例中的 Key 和资源 ID 都是占位。各产品不能共用同一个服务密钥变量；未配置服务密钥的产品无法被匹配。缺少专用方舟 Key 时该产品的 Seedream 调用明确失败（`config_missing`），不借用其他产品或旧共享 Key。

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

在服务器通过受控运维权限查询（`legacy` 仅用于读取历史旧来源记录）：

```bash
docker exec image-service python -m src.usage_report --app-id legacy
docker exec image-service python -m src.usage_report --app-id fusion --since 2026-10-23T00:00:00+08:00 --until 2026-10-24T00:00:00+08:00
```

备份请使用 SQLite 的在线 Backup API，或停止写入后复制数据库；WAL 工作期间不能只复制主 `.sqlite3` 文件并假定记录完整。数据库目录必须保留在持久卷中，备份不包含方舟密钥。

## 费用口径与上线边界

方舟已成功返回图片的尝试按记录的 wire model、输出像素与参考图数量估算，价格版本为 `ark-2026-10-23`。未知模型、缺失像素、Gemini/Qwen 等未定价响应明确记为未定价；待定的超时、断线、取消也需与官方账单核对。额外参考图检索、判断模型、Fusion 聊天模型等费用不属于这份生图估算。

官方实扣按专用 Key 资源 ID 的分账账单确认：[官方分账说明](https://docs.volcengine.com/docs/ark/practice-guidance-for-modelark-bill-split?lang=zh)。服务记录不包含优惠、赠额和账单调整，不伪装成官方实扣。

dev 已统一采用产品服务密钥，旧调用方不再保留兼容。旧图片和过去不完整的日志不回填虚构产品身份。历史数据是否属于 Fusion，仍保持原来的证据边界。

## REST / MCP 统一限流与并发

所有有效生图请求都在共享生成服务中检查以下三层限制；REST 与 MCP、同一产品的多个服务实例共用 Redis 额度。模型读取、MCP 初始化/工具发现及用量查询不消耗生图额度。

| 范围 | 每窗口调用数 | 同时执行数 |
|---|---|---|
| 全局 | `RATE_LIMIT_GLOBAL=20` | `MAX_CONCURRENT_GLOBAL=6` |
| 产品 | `RATE_LIMIT_PRODUCT=20` | `MAX_CONCURRENT_PRODUCT=4` |
| 产品内主体 | `RATE_LIMIT_USER=5` | `MAX_CONCURRENT_USER=2` |

窗口为 `RATE_LIMIT_WINDOW=60` 秒，按 Redis 时间划分固定窗口；窗口边界附近可出现相邻两窗口额度集中到达，不是滑动窗口。窗口和各项限制必须为正数。用户字段省略时，计入该产品的服务主体；不同产品、用户与服务主体分别计数。持有服务密钥的产品后端负责保证用户 ID 可信；用户和场景不能绕过产品/全局上限。IP 和 X-Forwarded-For 不再决定额度。

缓存命中也算一次调用并短暂占用并发槽，但不增加上游费用。no_cache、换场景或从 REST 改调 MCP 不重置额度。同产品的相同参数等待去重结果时也占用并发槽；超限直接拒绝，不排队。被拒绝的请求不消耗其他维度的额度，不提交上游；用量记录保留拒绝错误与零次上游尝试。

REST 超限返回 HTTP 429、`error=rate_limited`、`retryable=true`、`retry_after` 秒数及 `Retry-After` 响应头。MCP 沿用结构化错误格式，在 `error` 内返回同样的错误码、可重试标记和 `retry_after`；工具调用本身仍使用正常 MCP 响应。频率超限建议等待当前窗口结束，并发超限建议至少等 1 秒；调用方不可无限自动重试。

Redis 的 WATCH/MULTI 将三层检查与占用原子提交。准入检查有 0.75 秒上限，Redis 故障或争用持续不解时返回 `admission_unavailable`（REST 503），不放行付费生成。准入与生成共同受总时限和断连取消控制。成功、失败、超时和取消均尝试在有界时间内只释放自己的并发占位；进程退出或清理失败时，租约在总生成预算加 5 秒后过期。Redis 被清空/数据丢失会重置额度，部署依赖现有共享 Redis 的可用性。

这里限制调用频率和并发，不提供日/月金额硬预算或用户用量查询筛选；这些可单独扩展。金额仍以实际用量和官方账单为准。
