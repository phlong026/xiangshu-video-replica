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

## 3. G0-01 门禁进度

- ✅ **查询响应结构已确认**：`items[]` + `total`，无官方 `task{}` 形状 → 仓库 1 的 items[] 解析为正确实现。
- ✅ **架构评审分叉裁决（响应部分）**：真实响应只有 `items[]`，仓库 1 **不需要**补 `task{}` 兼容；仓库 2 `extract_task` 的 `task{}` 分支是对 MiniMax 官方形状的冗余兼容，保留无害。
- ⏳ 仍待证据：查询**请求** URL 形状（`?task_id=` vs 路径参数）、取消/回调能力、失败状态全集（`failed`/`cancelled` 等）、H3 结果 MP4 的原生音轨可播放性。
