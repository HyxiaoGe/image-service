# Seedream 图片编辑调研

日期：2026-10-23（北京时间）。项目基线：`d9e3fa8`。

## 结论

**支持，且项目现有 REST / MCP 接口已经能够完成普通图片编辑、提示词坐标编辑和连续编辑，不必先新增编辑接口。**

Seedream 在同一个 `POST /api/v3/images/generations` 接口中接收 `prompt` 和 `image`，同时处理生成与编辑。项目的 `reference_images` 已转换为图片数据 URI，单张传 `image` 字符串、多张传数组。Flash 与 Pro 都支持该路径。

本次没有修改生成逻辑、MCP 工具定义或线上配置。完成官方资料核对、现有代码检查，以及四张真实编辑结果的逐图检查。

## 能力与项目支持情况

| 能力 | 模型能力 | 本项目现状 |
|---|---|---|
| 基于原图修改内容、颜色、背景 | Flash / Pro 支持图文输入 | 已支持；本次验证换背景，前次已验证换色 |
| 点选 / 框选局部编辑 | 提示词包含 `<point>` / `<bbox>` | 已可直接通过 `prompt` 使用；本次验证框选加图案 |
| 多参考图融合、跨图片编辑 | Flash / Pro 最多 10 张参考图 | 代码支持多图，本项目上限 3 张；本次未实测跨图编辑 |
| 连续修改同一图片 | 可将上次结果作为下次输入 | 已支持；本次验证加星标后再移除 |
| 图层拆分 | 可输出底图和多个独立图层 | 未开放；请求参数和返回数组都需适配 |
| 带透明通道的编辑输出 | 有条件支持 | 未开放 `background` / `output_format` 参数 |
| 独立 `mask` / `strength` 参数 | 当前所查官方 API 未列出这类参数 | 项目未定义，不能把框选标签当像素遮罩 |

资料来源：[官方图片生成 API](https://docs.volcengine.com/docs/ark/image-generation-api?lang=zh)、[Pro / Flash 官方教程](https://docs.volcengine.com/docs/ark/seedream-5-0-pro?lang=zh)、[交互编辑指南](https://docs.volcengine.com/docs/ark/seedream-5-0-pro-editing-guide?lang=en)。

## 现有接口如何调用

MCP 继续使用 `generate_image`，REST 继续使用 `POST /v1/generate`。只要传入原图 URL 和明确的修改指令即可：

```json
{
  "prompt": "编辑图1：把白色背景改成靠窗木桌和虚化绿植。保留蓝色杯子的轮廓、把手和拍摄角度。",
  "reference_images": ["https://images.example.com/original.jpg"],
  "quality": "standard",
  "aspect_ratio": "1:1",
  "size": "1024x1024"
}
```

示例 URL 需要替换成可访问的原图 URL，或本服务之前返回的完整 `image_url`。每次编辑返回新图片 URL，不覆盖原文件。

默认 Flash 已可处理常规编辑；明确优先精修质感时可用 `quality="high"` 选择 Pro。不能因为有参考图或局部修改就一律升到 Pro。

### 坐标编辑

官方交互协议把位置写在 `prompt` 中，不需要新增 `mask` 字段。例如：

```json
{
  "prompt": "只编辑图1 <bbox>400 420 610 620</bbox> 区域：在蓝色杯子正面添加金色五角星贴花，尽量保持其他部分不变。",
  "reference_images": ["https://images.example.com/original.jpg"],
  "quality": "standard",
  "aspect_ratio": "1:1",
  "size": "1024x1024",
  "fallback_model": "none"
}
```

- 点选：`<point>x y</point>`。
- 框选：`<bbox>x1 y1 x2 y2</bbox>`。
- 坐标范围为 0–999，基于整张参考图归一化，左上角为原点；多图时说明“图1”“图2”来定位输入。
- 从展示坐标换算时，需要先去除画布缩放、平移和留白，再归一化并截断到 0–999。不能直接把鼠标的浏览器窗口坐标传给模型。

这些协议来自[官方交互编辑指南](https://docs.volcengine.com/docs/ark/seedream-5-0-pro-editing-guide?lang=en)。本项目没有过滤提示词中的这些标签，可以原样传给 Seedream。

`fallback_model="none"` 用于本次验收，也适合明确依赖 Seedream 坐标协议的调用。现有跨供应商备用没有校验坐标标签语义，不应假设所有备用模型都按相同协议解释。

### 连续编辑

第一轮传原图 URL，第二轮把第一轮返回的 `image_url` 放入 `reference_images`，并描述本轮要修改的内容。服务没有隐式会话记忆，每轮必须明确传入图片。相同参数默认可命中缓存；需要重新出一版时使用 `no_cache=true`。

## dev 实测

原图为已有的蓝色陶瓷杯白底图：`5333bb0c-1189-4080-b6c0-43b8dd331514.jpeg`，1024×1024。

通过已部署 MCP 的 `generate_image` 调用，统一 `size=1024x1024`、`aspect_ratio=1:1`、`fallback_model=none`、`no_cache=true`。前 3 张直接引用原图；第 4 张引用第 2 张生成的结果。

| 任务 | 模型 | 服务耗时 | 目视结果 |
|---|---|---:|---|
| 白底改成窗边木桌与绿植背景 | Flash | 8.42 秒 | 背景、桌面和接触阴影改变，蓝杯轮廓与右侧把手基本保留 |
| 在指定框内添加金色星标 | Flash | 6.96 秒 | 星标出现在杯身正面目标区域，呈立体金属徽章质感 |
| 相同框选、相同修改指令 | Pro | 19.26 秒 | 星标出现在杯身正面目标区域，更接近平面贴花 |
| 将 Flash 添加的星标移除 | Flash | 7.31 秒 | 星标消失，恢复为连续蓝色釉面；主要构图和把手基本保留 |

四次日志均确认：实际模型符合 `quality` 路由、只发起 1 次上游调用、使用 1 张参考图、输出为 1024×1024。全部成功，未走备用。耗时取 `generation_finished.duration_ms`，不用于推导普遍时延或成功率。

每个测试只有一个样本，且为简单单物体场景。尚未验证复杂遮挡、多主体身份保持、跨图迁移、人物换装、文本精确替换、透明输出和图层拆分；不能把这四张结果当作所有编辑任务的质量保证。

| 本地文件 | dev 生成 ID |
|---|---|
| `background-flash.jpeg` | `49f4fac2-2c08-4922-a9dd-5ea9901eac4b` |
| `bbox-flash.jpeg` | `eb54fa2d-f5d9-4f3c-9460-65b6e32c90dd` |
| `bbox-pro.jpeg` | `b88326cb-661c-4900-81b0-6b902cbe4e7a` |
| `remove-flash.jpeg` | `424e215e-3187-45a5-9771-e07f229db651` |

原始结果和请求 manifest 保存在本地 `storage/images/validation/editing-20261023/`，不纳入 Git。

## 需要明确的边界

1. **编辑属于生成式重绘。** 点 / 框坐标帮助定位，不是保证框外像素完全不变的遮罩。对商标、产品几何或版式有严格要求时，需要额外验收；像素级不变要求可能还需确定性合成流程。
2. **输出比例不会自动跟随输入。** 当前默认 `aspect_ratio=1:1`，编辑横图 / 竖图时应显式传原图比例和所需像素预算，避免无意改变构图。
3. **目前输入是 URL。** `reference_images` 不接受本地文件路径或 Base64 字符串；图片从本服务静态目录读取或经远端下载验证，最多 3 张、PNG/JPEG/WebP、每张最多 4 MB。支持用户直接上传图片还需要文件上传 / URL 转换链路。
4. **透明编辑有输入条件。** 官方 `background=transparent` 要求图生图、单张已有透明通道的输入，输出 PNG；不能由此承诺任意 JPEG 可以直接透明抠图。项目暂未开放该参数。来源：[官方图片 API](https://docs.volcengine.com/docs/ark/image-generation-api?lang=en)。
5. **图层不是一个普通图片字段。** 原生可返回 1 张底图和最多 16 个图层；项目当前只解析 `data[0]`，响应也只有一个 `image_url`，不能直接转发 `layer_decomposition=true` 就视为完成适配。需要多文件存储、图层顺序 / 位置元数据以及 REST / MCP 返回契约。来源：[官方 Pro / Flash 教程](https://docs.volcengine.com/docs/ark/seedream-5-0-pro?lang=zh)。

## 建议推进方式

第一步只补充 MCP 编辑使用说明与示例，明确“原图 URL + 修改指令”、输入顺序、坐标协议、原比例和连续编辑方式。基础功能已经可用，无需重新设计供应商调用或强制增加 `edit` 开关。

若实际调用方经常把“参考生成”和“修改原图”混淆，可以再增加一个更容易发现的 `edit_image` 工具，作为同一生成服务的薄封装；这属于调用体验优化，而非模型支持编辑的前提。

上传图片、结构化选区、透明通道和图层输出按产品需要分别适配。保持使用现有的总等待预算与取消机制，不为编辑另外开启无限等待或重复提交。
