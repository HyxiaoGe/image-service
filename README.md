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
    <td align="center"><img src="docs/images/lighthouse-nano-banana-2.1.jpg" alt="gemini-nano-banana-2.1"><br><sub><code>gemini-nano-banana-2.1</code>（默认备用）</sub></td>
    <td align="center"><img src="docs/images/lighthouse-gemini-3.1-flash-image.jpg" alt="gemini-3.1-flash-image"><br><sub><code>gemini-3.1-flash-image</code></sub></td>
    <td align="center"><img src="docs/images/lighthouse-gemini-3-pro-image.jpg" alt="gemini-3-pro-image"><br><sub><code>gemini-3-pro-image</code></sub></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/images/lighthouse-seedream-5-0-pro.jpg" alt="doubao-seedream-5-0-pro"><br><sub><code>doubao-seedream-5-0-pro</code>（按需选择）</sub></td>
    <td align="center"><img src="docs/images/lighthouse-seedream-5-0-flash.jpg" alt="doubao-seedream-5-0-flash"><br><sub><code>doubao-seedream-5-0-flash</code>（默认）</sub></td>
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
| 🛡️ **防护** | 产品服务密钥鉴权、全局 / 产品 / 用户限流和并发控制、参考图只允许公网地址 |
| 📈 **可观测** | 每次请求一条结构化汇总日志，Prometheus 指标在 `/metrics` |

## 目录

- [快速开始](#快速开始)
- [切换主力 / 备用模型](#切换主力--备用模型)
- [支持的模型](#支持的模型)
- [Seedream 的尺寸](#seedream-的尺寸)
- [参考图（reference_images）](#参考图reference_images)
- [图片编辑](#图片编辑)
- [版本与 MCP 调用方更新](#版本与-mcp-调用方更新)
- [自动找参考图（reference_subject）](#自动找参考图reference_subject)

## 快速开始

**接口一览**

| 方式 | 入口 |
|---|---|
| REST | `POST /v1/generate`、`GET /v1/models`、`GET /v1/health` |
| MCP | `generate_image`、`list_models`（路径 `/mcp`） |

产品身份、多方舟 Key、持久化用量和统一限流见[产品归因接入说明](docs/PRODUCT_ATTRIBUTION.md)。各产品用自己的服务密钥调用、由密钥决定产品和方舟 Key；默认且仅支持严格产品密钥认证，匿名和旧共享 Key 均被拒绝。

**出一张图**

```bash
curl -s http://localhost:8090/v1/generate \
  -H "Authorization: Bearer $FUSION_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "A lighthouse on a rocky coast at sunset, photorealistic", "aspect_ratio": "16:9"}'
```

```json
{
  "id": "…",
  "image_url": "/static/images/<id>.jpeg",
  "model": "doubao-seedream-5-0-flash",
  "requested_model": "doubao-seedream-5-0-flash",
  "fallback_used": false,
  "grounding_used": false,
  "cached": false,
  "created_at": "…",
  "reference_images_used": [],
  "reference_search": null
}
```

REST 返回的 `image_url` 是相对路径，拼上服务地址即可访问；MCP 返回的已经是完整 URL。REST 和 MCP 均须携带产品服务密钥；产品身份不由工具参数决定。其他常用参数：`model`、`quality`、`size`、`style`、`fallback_model`、`grounding`、`reference_images`、`reference_subject`、`no_cache`，完整说明见 `/docs`（FastAPI 自动生成）。

### 按画质偏好选择模型

REST 和 MCP `generate_image` 都支持可选的 `quality`，由调用方根据用户偏好传入：

| 参数 | 初始模型 | 使用场景 |
|---|---|---|
| `quality="standard"` | Seedream 5.0 Flash | 日常配图、草稿、普通海报、常规参考图编辑 |
| `quality="high"` | Seedream 5.0 Pro | 用户明确要求精修成稿、优先质感，并接受更高费用和等待时间 |
| 不传 `quality` 和 `model` | `DEFAULT_MODEL` | 保持现有默认行为 |

显式 `model` 优先于 `quality`；Lite 继续通过 `model` 显式选择。`quality` 只决定初始模型，分辨率由 `size` 单独控制。中文、参考图或较长提示词本身不要求 `high`。

例如：`{"prompt":"制作一张咖啡活动海报","quality":"high","aspect_ratio":"3:4","size":"1536x1536"}`。

现有备用规则继续生效：`high` 不保证最终一定由 Pro 出图；需要严格指定模型时可同时传 `fallback_model="none"`。返回的 `requested_model` 是解析后的初始模型，`model` 是实际出图模型。日志 `quality` 与 `model_source`（`model` / `quality` / `default`）记录选择依据；缓存按解析后的模型区分，同一模型的等价请求共享缓存。

**本地开发**

```bash
cp .env.example .env                                                  # 填 PRODUCTS / 产品服务密钥与方舟 Key / LITELLM_* / DASHSCOPE_API_KEY / REDIS_URL
uv run uvicorn src.main:create_app --factory --reload --port 8090    # 启动
uv run pytest                                                         # 测试
ruff check . && ruff format .                                         # lint + 格式化
python scripts/check_architecture.py                                  # 分层依赖检查
```

架构和代码约定见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)、[`docs/CODING_CONVENTIONS.md`](docs/CODING_CONVENTIONS.md)。

## 切换主力 / 备用模型

调用方不传 `model` 和 `quality` 时用**默认模型**，不传 `fallback_model` 时用**默认备用模型**。显式 `model` 优先于 `quality`，`quality` 优先于默认模型。

| 环境变量 | 当前取值 | 作用 |
|---|---|---|
| `DEFAULT_MODEL` | `doubao-seedream-5-0-flash` | 默认出图模型 |
| `DEFAULT_FALLBACK_MODEL` | `gemini-nano-banana-2.1,qwen-image-3.0` | 备用模型候选列表（逗号分隔，按顺序尝试）；留空表示不启用默认备用 |

切换步骤（不需要改代码）：

```bash
ssh dev
cd ~/project/image-service
vi .env                       # 修改 DEFAULT_MODEL / DEFAULT_FALLBACK_MODEL
docker compose up -d --force-recreate   # 重新创建容器才会加载新的 .env；单纯 restart 不会
curl -s localhost:8090/v1/health
```

例如使用 Flash 当主力、Gemini 和 Qwen 当备用（默认配置）：

```
DEFAULT_MODEL=doubao-seedream-5-0-flash
DEFAULT_FALLBACK_MODEL=gemini-nano-banana-2.1,qwen-image-3.0
```

说明：

- 所有值必须是 `GET /v1/models` 里的模型 ID；备用列表不能包含 `DEFAULT_MODEL`，也不能重复。填错时服务启动失败，日志会写明是哪个变量。
- 未设置 `DEFAULT_FALLBACK_MODEL` 时，内置备用链会去掉已经选作默认主模型的那一项，兼容仍显式使用 Gemini 的现有部署；显式配置的备用列表仍按上一条严格校验。
- 触发换下一个模型的情况：被内容审核拦截（`content_filtered`），或上游不可用（`upstream_unavailable`：402 余额用尽、方舟 403 欠费错误、429、5xx、连不上）。按列表顺序逐个尝试，某个成功就停；全部失败时返回最后一个模型的错误，`fallback_from` 写明主模型最初的失败原因。
- 生图提交后的读写超时或单模型期限耗尽：不重发同一模型；有备用且总预算仍足够时尝试下一个模型。已提交的超时请求仍可能在供应商侧继续生成和计费，切备用可能产生额外费用；传 `fallback_model="none"` 可关闭备用。
- 请求参数错误、响应途中连接异常，以及 Qwen 已生成图片后的结果下载失败 / 超时，不继续生成备用图片。建立连接前的超时和连接失败仍最多重试两次，但包含在单模型和请求总预算内。
- 列表里不适用的模型会自动跳过，不报错：调用方选的模型恰好就是它，或者它不支持所请求的比例（例如 Seedream 主力请求 7:5，Gemini 备用不支持）。
- 调用方传 `fallback_model` 时只用它这一个模型（同样的触发规则）；传 `"none"` 关闭本次请求的备用。
- 备用列表参与缓存 key，所以修改 `DEFAULT_FALLBACK_MODEL` 后，已有缓存不会再被命中，同样的请求会重新生成一次。
- 返回里的 `model` 是实际出图的模型，`requested_model` 是请求的模型，`fallback_used` 表示是否走了备用；日志 `fallback_tried` 记录依次失败的模型和原因。

### 总等待预算与取消

REST 和 MCP 共用一条截止时间。默认总预算 **135 秒**，包含缓存 / 去重锁等待、参考图下载与搜索、所有主备模型调用、重试退避和清理。它小于 Fusion 的 150 秒调用上限；修改服务配置时应继续为调用方和网络传输留出余量。

| 环境变量 | 默认值 | 含义 |
|---|---:|---|
| `GENERATION_TIMEOUT_SECONDS` | 135 | 整次请求预算 |
| `MODEL_TIMEOUT_SECONDS` | 60 | 普通模型一次执行的上限，含连接重试和结果下载 |
| `SEEDREAM_PRO_TIMEOUT_SECONDS` | 105 | Seedream Pro 的执行上限 |
| `FALLBACK_RESERVE_SECONDS` | 30 | 有备用时给备用预留的最少时间；实际预留取它与首个备用自身执行上限中的较大值 |
| `MIN_MODEL_BUDGET_SECONDS` | 5 | 不足此时间时，不再启动一个模型 |

模型实际可用时间取配置上限与剩余预算中的较小值；首个模型还要扣除备用预留，预留取 `FALLBACK_RESERVE_SECONDS` 与首个备用自身执行上限中的较大值，让备用能用满自己的时间（例如 Pro 卡住后 Gemini 仍有 60 秒，而不是只剩 30 秒）。扣除预留后首个模型已不足 `MIN_MODEL_BUDGET_SECONDS` 时不再预留，先让它用完剩余时间。总预算内最多预留 1 秒做有界锁清理，所以在无前处理耗时、默认备用为 Gemini 的配置下，Pro 首次最多约 74 秒、备用 60 秒；普通模型首次仍为 60 秒，备用同样有 60 秒。备用继续按列表顺序尝试，剩余预算不足时停止。总期限耗尽返回 `upstream_timeout`（HTTP 504），不会再额外等待一个完整模型周期。

HTTP 断连会通过原始 ASGI 请求的共享状态传给生成任务，包括由 MCP SDK 生命周期任务组执行的工具。MCP `notifications/cancelled` 和直接任务取消同样会终止本地等待及后续调用；取消不会被转成内部错误或触发备用。清理只释放本请求持有的锁，失败时由有限 TTL 兜底。

停止本地 HTTP 等待不能保证供应商取消或退费；已经完成的图片及缓存也可能保留。日志 `total_budget_seconds`、`model_budget_seconds`、`stage`、`cancel_reason` 和 `model_timed_out.maybe_billed` 可用于区分期限耗尽、调用方取消和潜在计费风险。

## 支持的模型

完整列表以 `GET /v1/models` 为准（含每个模型支持的比例和是否支持搜索增强）。

| 模型 ID | 上游 | 说明 |
|---|---|---|
| `gemini-nano-banana-2.1` | Gemini | 默认第一备用；支持网页 + 图片搜索 |
| `gemini-3.1-flash-image` | Gemini | Nano Banana 2 稳定版；支持网页 + 图片搜索 |
| `gemini-3-pro-image` | Gemini | Nano Banana Pro 稳定版；只支持网页搜索 |
| `doubao-seedream-5-0-pro` | 火山方舟 `doubao-seedream-5-0-pro-260628` | 画质优先，按需选择；输出分辨率影响计费档位 |
| `doubao-seedream-5-0-flash` | 火山方舟 `doubao-seedream-5-0-flash-260915` | 默认主力，优先速度和成本 |
| `doubao-seedream-5-0-lite` | 火山方舟 `doubao-seedream-5-0-260128` | 官方计划于 2026-11-24 下线 |
| `qwen-image-3.0-pro` | 阿里云百炼 | 中文文字、长 prompt 排版强；支持 1–3 张参考图；需要 `DASHSCOPE_API_KEY` |
| `qwen-image-3.0` | 阿里云百炼 | 3.0 标准版，更便宜 |
| `doubao-seedream-4-5` / `doubao-seedream-4-0` | 火山方舟，由 `ARK_MODEL_ID` 决定（默认 4.5） | 两个别名实际调用同一个版本；当前默认版本计划于 2026-11-24 下线 |
| `gemini-3.1-flash-image-preview`、`gemini-3-pro-image-preview`、`nano-banana-pro-preview` | Gemini | 预览版，已过 Google 公布的最早下线日期（2026-06-25），随时可能停用，请换成上面的稳定版 |
| `gemini-2.5-flash-image` | Gemini | 计划 2027-03-15 下线；不支持搜索 |

## Seedream 的尺寸

`aspect_ratio` 决定形状，支持 `1:16` 到 `16:1`；`size` 的宽×高决定像素预算，默认 `1024x1024`。服务按模型上下限及像素对齐调整尺寸，因此返回图片不一定与 `size` 的两边完全相同。

| 模型 / 比例 | 请求 size | 发给方舟的尺寸 |
|---|---|---|
| Pro / Flash，1:1 | `1024x1024` | `1024x1024` |
| Pro / Flash，16:9 | `1024x1024` | `1376x768` |
| Pro / Flash，16:9 | `1536x1536` | `2048x1152` |
| Pro / Flash，8:1 | `1536x1536` | `4352x544` |
| 4.x / Lite，16:9 | `1024x1024` | `2560x1440`（达到旧模型的最低像素要求） |

Pro 的低分辨率预算在对齐后仍保持不超过 261 万像素，避免无意进入更贵的档位；传 `2048x2048` 可使用更高的像素预算。Flash 不按这两档区分每张价格。最新价格见[方舟官方价格](https://docs.volcengine.com/docs/ark/model-pricing?lang=zh)，版本下线时间见[官方公告](https://docs.volcengine.com/docs/ark/model-deprecation-notice?lang=en)。

Pro / Flash 不支持原生联网搜索，`grounding=true` 在这两个模型上不生效。需要真实物件外观时，可使用 `reference_images` 或 `reference_subject`。日志 `ark_model`、`ark_size`、`ark_output_size`、`ark_usage` 和 `ark_request_id` 记录方舟实际版本、输入输出规格、用量及追踪信息。

已部署服务的 `.env` 若显式设置了旧 `DEFAULT_MODEL`，仍优先采用该值；升级代码后需要同步修改配置并重建容器。

## 参考图（reference_images）

`POST /v1/generate` 和 MCP `generate_image` 都支持 `reference_images`：一组图片 URL，模型会参照这些图出图，比如照着真机外观画，或在某张图的基础上修改。

- 最多 3 张，每张不超过 4 MB，支持 PNG、JPEG、WebP。服务会下载这些图，再以 base64 内联的方式传给上游：3 × 4 MB 编码后约 16 MB，在 Gemini 单次请求 20 MB 的内联上限以内。每多一张图，请求就多一次下载和更大的上游请求。
- 只接受公网的 http(s) 地址。指向内网、本机或链路本地地址的 URL 会被拒绝，重定向的每一跳都会检查。
- 服务器走 fake-ip DNS 代理（如 Clash）时，本地解析结果是 `198.18.0.0/15`，这时会改用 DNS-over-HTTPS（Cloudflare）查询真实 IP 再判断；DoH 失败或超时一律拒绝。本地解析出真实 IP 时不额外查询。
- 本服务生成的图片 URL（`SERVICE_BASE_URL` + `/static/images/...`）直接从本地存储读取，不走网络。
- 下载失败、拿到的不是图片（例如网页）、超过大小上限时，返回 `invalid_params`，`fields=["reference_images"]`，message 会写明是第几张、什么原因。
- 参考图 URL 会计入缓存 key。同一个 URL 背后的图片如果换了，缓存过期前会一直返回旧结果，需要重新生成时传 `no_cache: true`。
- 主模型被拦截、换备用模型重试时，沿用已下载的参考图，不会重新下载。


## 图片编辑

编辑继续使用同一个 REST 接口或 MCP `generate_image` 工具，传原图 URL 和修改指令：

```json
{
  "prompt": "把图1的白色背景换成窗边木桌与虚化绿植，保留杯子轮廓、把手和拍摄角度。",
  "reference_images": ["https://images.example.com/original.jpg"],
  "quality": "standard",
  "aspect_ratio": "1:1",
  "size": "1024x1024"
}
```

示例 URL 需替换为可访问的原图 URL，也可使用本服务之前返回的完整 `image_url`。每轮编辑产生新 URL；下一轮把该 URL 放回 `reference_images`，服务不会隐式保留图片会话。保持原图形状时应显式传 `aspect_ratio`，默认 1:1 不会自动跟随原图。

Seedream Flash / Pro 均可在 `prompt` 中使用 `<point>x y</point>` 或 `<bbox>x1 y1 x2 y2</bbox>` 定位编辑区域，坐标相对于整张参考图归一化到 0–999。例如：`只编辑图1 <bbox>400 420 610 620</bbox> 区域，在杯身添加金色星标，尽量保留其他部分。` 依赖该坐标协议时建议设置 `fallback_model="none"`，避免跨供应商备用对语法的解释不同。

坐标定位不是像素遮罩，不保证区域外像素完全不变。当前工具没有开放本地文件上传、`mask` / `strength`、多图层返回或透明输出控制。实测与能力边界见[编辑调研](docs/SEEDREAM_EDITING_RESEARCH.md)。

## 版本与 MCP 调用方更新

发布记录见 [GitHub Releases](https://github.com/HyxiaoGe/image-service/releases)。服务版本同时出现在 MCP 初始化结果的 `serverInfo.version`、`GET /v1/health` 和 OpenAPI 的 `info.version`。

GitHub Release 面向集成人员，不会自动刷新 MCP 调用方缓存的工具定义。升级服务后，调用方应重新执行 `initialize` 和 `tools/list`，替换旧的工具描述与参数 schema，再将新定义提供给模型；只重连但继续使用旧缓存也不会让模型获得新用法。当前服务为 stateless HTTP，跨部署的刷新不依赖长期会话通知。

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
