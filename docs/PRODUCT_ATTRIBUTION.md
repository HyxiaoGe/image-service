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

REST `/v1/generate`、`/v1/models`、`/v1/usage`、`/v1/billing` 和整个 HTTP `/mcp` 端点均要求产品服务密钥，缺失、无效或误配到多个产品的相同密钥返回 401。用量和官方账单只能查该密钥所属产品。内存/stdio 生图和模型读取不提供匿名绕过。

`/v1/health`、`/metrics`、OpenAPI 和现有静态图片 URL 的公开行为保持原样；本次接入不提供图片访问权限隔离。鉴权拒绝发生在生图上游调用之前。

## 配置

平台配置使用 `<平台>_<用途>`，产品专用上游 Key 使用 `<平台>_<产品>_API_KEY`。`ARK` 表示火山方舟，`DASHSCOPE` 表示阿里百炼，`LITELLM` 表示代理平台。产品访问 image-service 的 `FUSION_SERVICE_KEY` / `VALIDATION_SERVICE_KEY` 不属于上游平台，保留产品前缀。`PRODUCTS` 中的 `ark_key_env` 引用相应环境变量名，`ark_resource_id` 仍是方舟 Key 的非秘密资源 ID。

| 配置用途 | 当前命名 |
|---|---|
| 方舟生图 | `ARK_BASE_URL`、`ARK_MODEL_ID`、`ARK_FUSION_API_KEY`、`ARK_VALIDATION_API_KEY` |
| 方舟官方账单 | `ARK_BILLING_ENABLED`、`ARK_BILLING_ACCESS_KEY`、`ARK_BILLING_SECRET_KEY`、`ARK_BILLING_REGION`、`ARK_BILLING_PRODUCT_CODE` |
| 百炼生图 | `DASHSCOPE_BASE_URL`、`DASHSCOPE_API_KEY` |
| LiteLLM 代理 | `LITELLM_BASE_URL`、`LITELLM_API_KEY` |

从旧配置升级时，将 `BILLING_*` 五项改名为 `ARK_BILLING_*`，`FUSION_ARK_API_KEY` / `VALIDATION_ARK_API_KEY` 改为 `ARK_FUSION_API_KEY` / `ARK_VALIDATION_API_KEY`，并同步修改 `PRODUCTS` 内的 `ark_key_env` 引用；值、别名和资源 ID 保持原值，重建容器生效。旧账单环境变量名不再读取，不保留两套配置。未来百炼账单可使用 `DASHSCOPE_BILLING_*`，产品 Key 可使用 `DASHSCOPE_<产品>_API_KEY`；当前尚未实现百炼官方账单查询。

在方舟创建产品专用 Key，并为每个产品生成独立的服务密钥（如 `openssl rand -hex 32`），配置在 image-service 宿主机 `.env`：

```dotenv
AUTH_MODE=required
PRODUCTS={"fusion":{"service_key_env":"FUSION_SERVICE_KEY","ark_key_env":"ARK_FUSION_API_KEY","ark_key_alias":"fusion","ark_resource_id":"<非秘密的Key资源ID>"},"validation":{"service_key_env":"VALIDATION_SERVICE_KEY","ark_key_env":"ARK_VALIDATION_API_KEY","ark_key_alias":"validation"}}
FUSION_SERVICE_KEY=<Fusion服务密钥>
ARK_FUSION_API_KEY=<Fusion专用方舟Key>
VALIDATION_SERVICE_KEY=<验证服务密钥>
ARK_VALIDATION_API_KEY=<验证专用方舟Key>
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

## 官方分账查询

`/v1/usage` 继续提供调用记录与标价估算。新增 REST 管理接口单独读取火山引擎官方分账，查询不会生成图片，不增加 MCP 工具参数：

```http
GET /v1/billing?bill_period=2026-10
Authorization: Bearer <该产品的服务密钥>
```

需在火山引擎开通按 API Key 分账，并将有账单只读权限的 IAM AK/SK 写入 image-service 宿主机 `.env` 后重建容器。这组凭证不同于方舟生图 API Key；不应提供给 Fusion、MCP 调用方或模型。已有产品配置必须填写对应 Key 的非秘密资源 ID `ark_resource_id=apikey-...`。

```dotenv
ARK_BILLING_ENABLED=true
ARK_BILLING_ACCESS_KEY=<火山引擎账单只读AK>
ARK_BILLING_SECRET_KEY=<火山引擎账单只读SK>
ARK_BILLING_REGION=cn-beijing
ARK_BILLING_PRODUCT_CODE=Doubao-Seedream
```

默认 `ARK_BILLING_ENABLED=false`。未启用、缺凭证或缺产品资源 ID 返回 503 `billing_not_configured`，不影响生图。AK/SK 使用 SecretStr 遮蔽配置表示，服务不记录签名、凭证或原始账单，只缓存白名单汇总。

调用官方 `ListSplitBillDetail`，使用 `Product=["Doubao-Seedream"]`、`SplitDimension="{\"Doubao-Seedream\":\"apikey_id\"}"` 和本产品 `SplitItemID`。2026-10-23 在官方 API Explorer 实测，该组合能读取 Fusion Key 的 0.12 元应付与实付；通用示例的 `ark_bd` 并非 Seedream 图片产品代码。调用方不能传资源 ID 或产品 ID 来查询其他产品；返回行也逐条核验产品代码、资源、账期及人民币币种。

返回示例中的金额是十进制字符串：

```json
{
  "app_id": "fusion",
  "ark_resource_id": "apikey-...",
  "bill_period": "2026-10",
  "cost_kind": "official_split_bill",
  "scope": "ark_api_key",
  "product_code": "Doubao-Seedream",
  "currency": "CNY",
  "status": "available",
  "record_count": 1,
  "payable_amount_cny": "0.12",
  "paid_amount_cny": "0.12",
  "expected_delay_days": 2,
  "settlement_finality": "not_asserted",
  "queried_at": "2026-10-23T12:18:35+00:00",
  "cached": false
}
```

`PayableAmount` / `PaidAmount` 分别为官方应付 / 实付，不推断最终结算。按 Key 汇总该账期全部 Seedream 图片费用，不能拆到用户、场景或单次 run；也不包括 Gemini/Qwen 等其他供应商费用。用户和场景仍看本地用量估算。同一个方舟 Key 若被其他系统共用，其图片费用也会进入该汇总；应保持产品专用。

账期支持当前月及过去 24 个月。官方说明分账相对明细约有 2 天延迟；当前月金额可能继续变化。没有返回行时，`status=no_data`，金额为 `null`，不把空账单解释为零费用。已清理的本地记录不会影响官方月度查询。

服务缓存每个凭证账号、产品、资源和账期的汇总 15 分钟，响应禁止浏览器共享缓存；`queried_at` 为官方查询时间，`cached` 标明是否复用。查询最多 10 秒、每页 100 行、最多 10 页，页间至少 250ms，所有产品和实例共用 Redis 查询锁。繁忙返回 503 `billing_busy` 和 `Retry-After: 1`；官方权限拒绝为 502 `billing_permission_denied`；分页不完整、警告或数据范围不符为 502 `billing_incomplete`；网络、总时限或 Redis 故障为 503 `billing_unavailable`。不返回部分金额，不自动重试或跟随上游重定向。

接口依据：[官方分账 API](https://api.volcengine.com/api-docs/view?action=ListSplitBillDetail&serviceCode=billing&version=2022-01-01)、[账单 OpenAPI 常见问题](https://docs.volcengine.com/docs/BillingCenter/BillingManagementOpenAPIFAQ?lang=zh)。

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
