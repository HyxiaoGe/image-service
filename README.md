# image-service

通用的 AI 生图服务：把 Gemini（经 LiteLLM Proxy）和 Seedream（火山方舟）包装成 REST 接口和 MCP 工具，带 Redis 缓存、限流和 Prometheus 指标。

- REST：`POST /v1/generate`、`GET /v1/models`、`GET /v1/health`
- MCP：`generate_image`、`list_models`（路径 `/mcp`）
- 本地开发、测试、架构约束见 `CLAUDE.md` 和 `docs/`

## 切换主力 / 备用模型

调用方不传 `model` 时用**默认模型**，不传 `fallback_model` 时用**默认备用模型**。调用方显式传入的值始终优先。

| 环境变量 | 当前取值 | 作用 |
|---|---|---|
| `DEFAULT_MODEL` | `gemini-nano-banana-2.1` | 默认出图模型 |
| `DEFAULT_FALLBACK_MODEL` | `doubao-seedream-5-0-pro` | 主模型被内容审核拦截（`content_filtered`）时换用的模型；留空表示不启用默认备用 |

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
DEFAULT_MODEL=doubao-seedream-5-0-pro
DEFAULT_FALLBACK_MODEL=gemini-nano-banana-2.1
```

说明：

- 两个值必须是 `GET /v1/models` 里的模型 ID，且不能相同；填错时服务启动失败，日志会写明是哪个变量。
- 备用模型只在 `content_filtered` 时触发，只重试一次；超时、上游不可用等错误不换模型。
- 默认备用模型不适用时会自动跳过，不报错：调用方选的模型恰好就是它，或者它不支持所请求的比例（例如 Seedream 主力请求 7:5，Gemini 备用不支持）。
- 调用方传 `fallback_model: "none"` 可以关闭本次请求的备用。
- 备用模型参与缓存 key，所以修改 `DEFAULT_FALLBACK_MODEL` 后，已有缓存不会再被命中，同样的请求会重新生成一次。
- 返回里的 `model` 是实际出图的模型，`requested_model` 是请求的模型，`fallback_used` 表示是否走了备用。

## 支持的模型

完整列表以 `GET /v1/models` 为准（含每个模型支持的比例和是否支持搜索增强）。

| 模型 ID | 上游 | 说明 |
|---|---|---|
| `gemini-nano-banana-2.1` | Gemini | 默认模型，Google 当前最新稳定版；支持网页 + 图片搜索 |
| `gemini-3.1-flash-image` | Gemini | Nano Banana 2 稳定版；支持网页 + 图片搜索 |
| `gemini-3-pro-image` | Gemini | Nano Banana Pro 稳定版；只支持网页搜索 |
| `doubao-seedream-5-0-pro` | 火山方舟 `doubao-seedream-5-0-pro-260628` | 默认备用模型 |
| `doubao-seedream-5-0-flash` | 火山方舟 `doubao-seedream-5-0-flash-260915` | 更快更便宜 |
| `doubao-seedream-5-0-lite` | 火山方舟 `doubao-seedream-5-0-260128` | |
| `doubao-seedream-4-5` / `doubao-seedream-4-0` | 火山方舟，由 `ARK_MODEL_ID` 决定（默认 4.5） | 两个别名实际调用同一个版本 |
| `gemini-3.1-flash-image-preview`、`gemini-3-pro-image-preview`、`nano-banana-pro-preview` | Gemini | 预览版，已过 Google 公布的最早下线日期（2026-06-25），随时可能停用，请换成上面的稳定版 |
| `gemini-2.5-flash-image` | Gemini | 计划 2027-03-15 下线；不支持搜索 |

## 参考图（reference_images）

`POST /v1/generate` 和 MCP `generate_image` 都支持 `reference_images`：一组图片 URL，模型会参照这些图出图，比如照着真机外观画，或在某张图的基础上修改。

- 最多 3 张，每张不超过 4 MB，支持 PNG、JPEG、WebP。服务会下载这些图，再以 base64 内联的方式传给上游：3 × 4 MB 编码后约 16 MB，在 Gemini 单次请求 20 MB 的内联上限以内。每多一张图，请求就多一次下载和更大的上游请求。
- 只接受公网的 http(s) 地址。指向内网、本机或链路本地地址的 URL 会被拒绝，重定向的每一跳都会检查。
- 本服务生成的图片 URL（`SERVICE_BASE_URL` + `/static/images/...`）直接从本地存储读取，不走网络。
- 下载失败、拿到的不是图片（例如网页）、超过大小上限时，返回 `invalid_params`，`fields=["reference_images"]`，message 会写明是第几张、什么原因。
- 参考图 URL 会计入缓存 key。同一个 URL 背后的图片如果换了，缓存过期前会一直返回旧结果，需要重新生成时传 `no_cache: true`。
- 主模型被拦截、换备用模型重试时，沿用已下载的参考图，不会重新下载。
