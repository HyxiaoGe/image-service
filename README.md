# image-service

通用的 AI 生图服务：把 Gemini（经 LiteLLM Proxy）和 Seedream（火山方舟）包装成 REST 接口和 MCP 工具，带 Redis 缓存、限流和 Prometheus 指标。

- REST：`POST /v1/generate`、`GET /v1/models`、`GET /v1/health`
- MCP：`generate_image`、`list_models`（路径 `/mcp`）
- 本地开发、测试、架构约束见 `CLAUDE.md` 和 `docs/`

## 切换主力 / 备用模型

调用方不传 `model` 时用**默认模型**，不传 `fallback_model` 时用**默认备用模型**。调用方显式传入的值始终优先。

| 环境变量 | 当前取值 | 作用 |
|---|---|---|
| `DEFAULT_MODEL` | `gemini-3.1-flash-image-preview` | 默认出图模型 |
| `DEFAULT_FALLBACK_MODEL` | `doubao-seedream-4-5` | 主模型被内容审核拦截（`content_filtered`）时换用的模型；留空表示不启用默认备用 |

切换步骤（不需要改代码）：

```bash
ssh dev
cd ~/project/image-service
vi .env                       # 修改 DEFAULT_MODEL / DEFAULT_FALLBACK_MODEL
docker compose up -d --force-recreate   # 重新创建容器才会加载新的 .env；单纯 restart 不会
curl -s localhost:8090/v1/health
```

例如让 Seedream 当主力、Gemini 当备用：

```
DEFAULT_MODEL=doubao-seedream-4-5
DEFAULT_FALLBACK_MODEL=gemini-3.1-flash-image-preview
```

说明：

- 两个值必须是 `GET /v1/models` 里的模型 ID，且不能相同；填错时服务启动失败，日志会写明是哪个变量。
- 备用模型只在 `content_filtered` 时触发，只重试一次；超时、上游不可用等错误不换模型。
- 默认备用模型不适用时会自动跳过，不报错：调用方选的模型恰好就是它，或者它不支持所请求的比例（例如 Seedream 主力请求 7:5，Gemini 备用不支持）。
- 调用方传 `fallback_model: "none"` 可以关闭本次请求的备用。
- 备用模型参与缓存 key，所以修改 `DEFAULT_FALLBACK_MODEL` 后，已有缓存不会再被命中，同样的请求会重新生成一次。
- 返回里的 `model` 是实际出图的模型，`requested_model` 是请求的模型，`fallback_used` 表示是否走了备用。
