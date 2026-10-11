<div align="center">

# image-service

**通用的 AI 生图服务**

把 Gemini（经 LiteLLM Proxy）和 Seedream（火山方舟）包装成统一的 REST 接口和 MCP 工具

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-FastMCP-6E56CF)
![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus-E6522C?logo=prometheus&logoColor=white)

</div>

## 效果

同一句 prompt：`A lighthouse on a rocky coast at sunset, photorealistic`，交给不同模型的出图结果：

<table>
  <tr>
    <td align="center"><img src="docs/images/lighthouse-nano-banana-2.1.jpg" alt="gemini-nano-banana-2.1"><br><sub><code>gemini-nano-banana-2.1</code>（默认）</sub></td>
    <td align="center"><img src="docs/images/lighthouse-gemini-3.1-flash-image.jpg" alt="gemini-3.1-flash-image"><br><sub><code>gemini-3.1-flash-image</code></sub></td>
    <td align="center"><img src="docs/images/lighthouse-gemini-3-pro-image.jpg" alt="gemini-3-pro-image"><br><sub><code>gemini-3-pro-image</code></sub></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/images/lighthouse-seedream-5-0-pro.jpg" alt="doubao-seedream-5-0-pro"><br><sub><code>doubao-seedream-5-0-pro</code>（默认备用）</sub></td>
    <td align="center"><img src="docs/images/lighthouse-seedream-5-0-flash.jpg" alt="doubao-seedream-5-0-flash"><br><sub><code>doubao-seedream-5-0-flash</code></sub></td>
    <td align="center"><img src="docs/images/lighthouse-seedream-5-0-lite.jpg" alt="doubao-seedream-5-0-lite"><br><sub><code>doubao-seedream-5-0-lite</code></sub></td>
  </tr>
</table>

## 功能

| | |
|---|---|
| 🎨 **多模型** | Gemini 稳定版与 Seedream 5.0 系列，一个接口切换；主模型被内容审核拦截时自动换备用模型 |
| 🖼️ **参考图** | `reference_images` 传图片 URL；`reference_subject` 自动搜真实照片作参考，让真实产品画得准 |
| 🔎 **搜索增强** | `grounding` 让 Gemini 先联网搜索再出图 |
| ⚡ **缓存** | Redis 缓存 7 天，同样的请求直接返回；并发相同请求只生成一次 |
| 🛡️ **防护** | Bearer token 鉴权、全局 / 单 IP 限流、参考图只允许公网地址 |
| 📈 **可观测** | 每次请求一条结构化汇总日志，Prometheus 指标在 `/metrics` |

## 目录

- [快速开始](#快速开始)
- [切换主力 / 备用模型](#切换主力--备用模型)
- [支持的模型](#支持的模型)
- [参考图（reference_images）](#参考图reference_images)
- [自动找参考图（reference_subject）](#自动找参考图reference_subject)

## 快速开始

**接口一览**

| 方式 | 入口 |
|---|---|
| REST | `POST /v1/generate`、`GET /v1/models`、`GET /v1/health` |
| MCP | `generate_image`、`list_models`（路径 `/mcp`） |

**出一张图**

```bash
curl -s http://localhost:8090/v1/generate \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "A lighthouse on a rocky coast at sunset, photorealistic", "aspect_ratio": "16:9"}'
```

```json
{
  "id": "…",
  "image_url": "/static/images/<id>.png",
  "model": "gemini-nano-banana-2.1",
  "requested_model": "gemini-nano-banana-2.1",
  "fallback_used": false,
  "grounding_used": false,
  "cached": false,
  "created_at": "…",
  "reference_images_used": [],
  "reference_search": null
}
```

REST 返回的 `image_url` 是相对路径，拼上服务地址即可访问；MCP 返回的已经是完整 URL。没有配置 `API_KEY` 时不需要 `Authorization` 头。其他常用参数：`model`、`size`、`style`、`fallback_model`、`grounding`、`reference_images`、`reference_subject`、`no_cache`，完整说明见 `/docs`（FastAPI 自动生成）。

**本地开发**

```bash
cp .env.example .env                                                  # 填 LITELLM_* / ARK_API_KEY / DASHSCOPE_API_KEY / REDIS_URL
uv run uvicorn src.main:create_app --factory --reload --port 8090    # 启动
uv run pytest                                                         # 测试
ruff check . && ruff format .                                         # lint + 格式化
python scripts/check_architecture.py                                  # 分层依赖检查
```

架构和代码约定见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)、[`docs/CODING_CONVENTIONS.md`](docs/CODING_CONVENTIONS.md)。

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
| `qwen-image-3.0-pro` | 阿里云百炼 | 中文文字、长 prompt 排版强；支持 1–3 张参考图；需要 `DASHSCOPE_API_KEY` |
| `qwen-image-3.0` | 阿里云百炼 | 3.0 标准版，更便宜 |
| `doubao-seedream-4-5` / `doubao-seedream-4-0` | 火山方舟，由 `ARK_MODEL_ID` 决定（默认 4.5） | 两个别名实际调用同一个版本 |
| `gemini-3.1-flash-image-preview`、`gemini-3-pro-image-preview`、`nano-banana-pro-preview` | Gemini | 预览版，已过 Google 公布的最早下线日期（2026-06-25），随时可能停用，请换成上面的稳定版 |
| `gemini-2.5-flash-image` | Gemini | 计划 2027-03-15 下线；不支持搜索 |

## 参考图（reference_images）

`POST /v1/generate` 和 MCP `generate_image` 都支持 `reference_images`：一组图片 URL，模型会参照这些图出图，比如照着真机外观画，或在某张图的基础上修改。

- 最多 3 张，每张不超过 4 MB，支持 PNG、JPEG、WebP。服务会下载这些图，再以 base64 内联的方式传给上游：3 × 4 MB 编码后约 16 MB，在 Gemini 单次请求 20 MB 的内联上限以内。每多一张图，请求就多一次下载和更大的上游请求。
- 只接受公网的 http(s) 地址。指向内网、本机或链路本地地址的 URL 会被拒绝，重定向的每一跳都会检查。
- 服务器走 fake-ip DNS 代理（如 Clash）时，本地解析结果是 `198.18.0.0/15`，这时会改用 DNS-over-HTTPS（Cloudflare）查询真实 IP 再判断；DoH 失败或超时一律拒绝。本地解析出真实 IP 时不额外查询。
- 本服务生成的图片 URL（`SERVICE_BASE_URL` + `/static/images/...`）直接从本地存储读取，不走网络。
- 下载失败、拿到的不是图片（例如网页）、超过大小上限时，返回 `invalid_params`，`fields=["reference_images"]`，message 会写明是第几张、什么原因。
- 参考图 URL 会计入缓存 key。同一个 URL 背后的图片如果换了，缓存过期前会一直返回旧结果，需要重新生成时传 `no_cache: true`。
- 主模型被拦截、换备用模型重试时，沿用已下载的参考图，不会重新下载。

## 自动找参考图（reference_subject）

传 `reference_subject`（例如 `"iPhone 18 Pro"`）时，服务会自动搜这个具体物件的真实照片，挑一张作为参考图传给模型。服务不会从 prompt 里猜，只在调用方明确传了这个参数时才找图。

流程：
1. 先查按 subject 的找图缓存（大小写和多余空格不影响命中）。
2. 没命中就用 Tavily 搜一次，搜索词是 `"{subject} official product photo"`。
3. 按规则筛掉：下载失败的、不是图片的、短边小于 600 的、内容重复的。最多下载 24 张，每个页面最多取 8 张，官方站点优先下载。
4. 剩下的最多 12 张交给视觉模型（`REFERENCE_JUDGE_MODEL`，默认 `gemini-3.8-flash`）一次判断。它先找出多数图片一致的外观，再逐张判断是否符合。提示词明确告诉它产品已经发布，不能凭训练知识判断产品存不存在。
5. 只从判断通过的图里选：完整背面优先（局部特写排后面），其次官方站点，最后看分辨率。默认 1 张（`REFERENCE_SEARCH_IMAGES`，最多 2 张）。来源域名只用来排序，不会让一张图直接通过。
6. 找到图时，出图 prompt 末尾固定加一句「参考图是 {subject} 的真实照片，外观以它为准。」，只说明这张图是什么、怎么用，不描述外观。实测不加这句时，模型基本不照参考图画。调用方自己传的 `reference_images` 不加这句。

返回结果：
- `reference_images_used`：实际传给模型的参考图，调用方传的排在前面，找到的排在后面。
- `reference_search`：`{status, cached, candidates, accepted}`。`status` 有四种：
  - `found`：找到了。
  - `none_accepted`：搜了但没有图通过。照常出图，调用方应如实告诉用户外观可能和真品不同。
  - `search_failed`：搜索或判断出错，或没配 Tavily key。同样照常出图。
  - `skipped`：调用方已经传满 3 张参考图，没有空位。

缓存：
- 找图结果：`found` 缓存 7 天，`none_accepted` 缓存 1 小时，`search_failed` 不缓存。传 `no_cache: true` 会跳过找图缓存，重新搜。
- 出图缓存的 key 包含 subject。没找到参考图时生成的图，在出图缓存里最多保留 1 小时（`search_failed` 时 5 分钟），之后的请求会重新找图。
- 缓存里记的图片 URL 如果已经失效，会删掉这条缓存，这一次按 `search_failed` 出图。

`TAVILY_API_KEYS` 和 reader-service 共用同一个号池，用逗号分隔。某个 key 返回 401 时停用到服务重启，返回 432/433（额度用完）时停用 1 天，返回 429 时停用 1 分钟，期间自动换下一个 key。留空就不找图。每次没命中缓存的找图，大约花 1 个 Tavily credit 和 $0.01–0.04 的判断费用，耗时约 10–20 秒。另外，每多带一张参考图，出图本身也会慢一些（实测 1 张图约 38 秒，不带图约 15 秒）。

已知局限：画面里有多台手机同框时，模型偶尔会把其中几台画回老款外观。
