# METASO H3 图生视频查询 — 真实响应样本

> 登记日期：2026-08-14
> 提供来源：用户提供（实际查询返回）
> 用途：G0-01 METASO H3 接口闭环的证据之一；已脱敏（任务 ID 与签名 URL 以占位符代替）
> 验证结论：仓库 1 `server/app/generation.py` 的查询解析与该响应结构完全兼容

## 1. 脱敏响应样本

```json
{
  "items": [
    {
      "id": "<任务ID>",
      "model": "MiniMax-H3",
      "title": "热气腾腾的拉面特写",
      "status": "succeeded",
      "created_at": 1786705621,
      "updated_at": 1786705715,
      "content": {
        "url": "<临时签名视频下载地址>"
      },
      "resolution": "768P",
      "duration": 5,
      "usage": {
        "total_seconds": 5,
        "input_seconds": 0,
        "output_seconds": 5,
        "input_image_count": 0
      },
      "ratio": "adaptive",
      "task_type": "generation",
      "modality": "video"
    }
  ],
  "total": 7
}
```

## 2. 对实现的影响

| 字段/形状 | 真实响应 | 仓库 1 实现（generation.py） | 结论 |
| --- | --- | --- | --- |
| 顶层形状 | `{"items": [...], "total": N}` | `_query_task` 解析 `payload["items"]`（255-268 行） | ✅ 匹配，无需改动 |
| 任务定位 | `items[].id` 精确匹配 | `item.get("id") == provider_task_id` | ✅ 匹配 |
| 成功状态 | `status: "succeeded"` | `if status == "succeeded": → SUCCEEDED`（227 行） | ✅ 匹配 |
| 结果 URL | `content.url`（临时签名地址） | `_metaso_content_url()` 校验 HTTPS 后归档 | ✅ 匹配 |
| 分辨率/时长/比例 | `resolution`/`duration`/`ratio` | `SUPPORTED_RESOLUTIONS`、4–15s、`adaptive` 契约 | ✅ 匹配 |

## 3. 创建接口证据（用户提供 cURL 示例，2026-08-14）

```bash
curl --request POST \
  --url https://metaso.cn/api/minimax/v2/video_generation \
  --header 'Authorization: Bearer <脱敏: 不记录真实密钥>' \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "MiniMax-H3",
    "content": [
      { "type": "text", "text": "<提示词>" },
      { "type": "image_url", "image_url": { "url": "<首帧URL>" }, "role": "first_frame" }
    ],
    "resolution": "2K",
    "duration": 5,
    "ratio": "adaptive"
  }'
```

响应：

```json
{ "task_id": "424010985738629" }
```

**与仓库 1 实现核对**：

| 项 | cURL 示例 | 仓库 1（generation.py） | 结论 |
| --- | --- | --- | --- |
| 创建端点 | `POST /api/minimax/v2/video_generation` | `METASO_CREATE_PATH="/api/minimax/v2/video_generation"` | ✅ 一致 |
| 请求体 | model/content(text+image_url first_frame)/resolution/duration/ratio | `build_h3_request()` 完全同构 | ✅ 一致 |
| 创建响应 | `{"task_id": "..."}` | `_metaso_task_id()` 读取 task_id | ✅ 一致 |

## 4. 能力与价格（用户提供控制台信息，2026-08-14）

- **取消 / 回调：不支持** —— 用户明确确认 METASO H3 接口不支持取消/回调。仓库 1 无 cancel 实现是**符合接口能力**的，不是缺口。
- 时长：4–15 秒（单个 ≤ 15.5 秒）。
- 分辨率：768P / 2K。
- 画面比例：自动 / 21:9 / 16:9 / 4:3 / 1:1 / 3:4 / 9:16。
- 图片素材：最多 9 张，前 5 张免费，超出按张计费（0.05 元/张）。
- H3 Context IR：0.05 元/次（默认开启，控制台可关）。
- 视频价格：2K 0.15 元/秒，768P 0.09 元/秒（MiniMax 官方价 2 折）。
- 计费：按生成时长与素材计费；充值基准 4399 元 = 500000 积分。

## 5. G0-01 门禁进度

- ✅ **创建接口确认**：端点、请求体、响应（task_id）与仓库 1 实现一致（见 §3）。
- ✅ **取消/回调确认**：不支持 → 无 cancel 实现为正确行为。
- ✅ **查询响应结构确认**：`items[]` + `total`，`status="succeeded"`，仓库 1 items 解析正确，无需补 `task{}` 兼容。
- ✅ **成功响应的状态与结果 URL 确认**：`succeeded` + `content.url`。
- ⏳ 仍待证据：查询**请求** URL 形状（`?task_id=` vs 路径参数）、失败状态全集（`failed`/`cancelled` 形状）、H3 结果 MP4 的原生音轨可播放性。
- ⚠️ **密钥安全**：用户示例中的 Bearer key 视作已暴露，未写入本仓库；须确认是否真实并吊销。
