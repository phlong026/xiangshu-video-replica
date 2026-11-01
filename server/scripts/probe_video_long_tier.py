"""16-30 秒长档付费探针——实证上游 v2 端点对 duration>15 的放行与计费口径.

要钉死的两个问题（对应 docs/30s视频生成实现说明-2026-11-01.md 的待授权验证项）：
1. 提交端点是否受理 duration=16–30 的 768P 任务（文生视频，最便宜路径）；
2. 「首帧图生 + 30 秒」组合是否被受理（决定前端是否需要加限制门禁）。

口径与 app.metaso_probe 一致：4xx/200 明确报错 = 未创建收费任务；超时/5xx/响应
不可识别 = 结果不确定（可能已计费）；受理成功即带回任务编号并只读回查一次。
不等待成片（成片要数分钟到数十分钟），探针的价值在「受理 + 计费档位回执」。

用法（在生产或任何已配置真实密钥的环境执行；密钥不落盘不进日志）：
    METASO_PROBE_API_KEY=... python scripts/probe_video_long_tier.py
    METASO_PROBE_API_KEY=... python scripts/probe_video_long_tier.py --skip-first-frame
首次实跑前建议先 --dry-run 只打印请求体（不发出任何调用）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError

# 允许从仓库内直接运行：scripts/ 与 app/ 同级。
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.external_calls import parse_provider_error  # noqa: E402
from app.generation import (  # noqa: E402
    H3ProviderFailed,
    MetasoH3Provider,
    SubmissionUncertain,
    UrllibMetasoHttpTransport,
    build_h3_request,
)

# 内容中性（与 app.metaso_probe 同一句式）：避免供应商内容审核干扰鉴权/计费验证。
PROBE_PROMPT = "一只白色的猫安静地坐在窗台上，看向窗外。"
# 公网 HTTPS 首帧（供应商要求素材 URL 必须公网 HTTPS）；无外网时可替换任意可公开
# 访问的图片直链。默认用一张稳定的公开占位图。
DEFAULT_FIRST_FRAME_URL = "https://picsum.photos/seed/metaso-probe/1280/720"


class ProbeOutcome:
    def __init__(self, name: str) -> None:
        self.name = name
        self.verdict = "UNKNOWN"
        self.detail = ""
        self.task_id: str | None = None
        self.read_back = ""

    def report(self) -> None:
        print(f"[{self.name}] 判定={self.verdict}")
        if self.task_id:
            print(f"[{self.name}] 任务编号={self.task_id} 回查={self.read_back}")
        print(f"[{self.name}] {self.detail}")


def _http_status(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            return None
        if isinstance(current, HTTPError):
            return current.code
        current = current.__cause__
    return None


def run_probe(
    provider: MetasoH3Provider,
    *,
    name: str,
    duration: int,
    first_frame_url: str | None,
    dry_run: bool,
) -> ProbeOutcome:
    request = build_h3_request(
        prompt_text=PROBE_PROMPT,
        first_frame_url=first_frame_url,
        duration_seconds=duration,
        resolution="768P",
        ratio="adaptive" if first_frame_url else "16:9",
    )
    outcome = ProbeOutcome(name)
    if dry_run:
        outcome.verdict = "DRY-RUN"
        outcome.detail = f"请求体（未发出）：{json.dumps(request, ensure_ascii=False)}"
        return outcome
    try:
        outcome.task_id = provider.submit_image_to_video(request)
    except SubmissionUncertain as exc:
        status = _http_status(exc)
        if status is not None and 400 <= status < 500:
            outcome.verdict = "REJECTED（未创建收费任务）"
            outcome.detail = f"供应商拒收 HTTP {status}；duration={duration} 不被受理。"
            return outcome
        code, message = parse_provider_error(None, allow_plain_text=False)
        _ = code
        if status is None and message:
            outcome.verdict = "REJECTED（未创建收费任务）"
            outcome.detail = f"供应商明确报错：{message}"
            return outcome
        outcome.verdict = "UNCERTAIN（可能已计费）"
        outcome.detail = f"提交结果未知（HTTP {status}）；需人工到供应商控制台核对。"
        return outcome
    except H3ProviderFailed as exc:
        outcome.verdict = "REJECTED（未创建收费任务）"
        outcome.detail = f"供应商明确拒绝：{exc}"
        return outcome
    outcome.verdict = "ACCEPTED（已受理，可能已计费）"
    outcome.detail = f"duration={duration} 被受理。"
    try:
        task = provider.query_image_to_video(str(outcome.task_id))
        outcome.read_back = str(getattr(task, "status", "unknown"))
    except H3ProviderFailed as exc:
        outcome.read_back = f"失败：{exc}"
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-key",
        default=os.environ.get("METASO_PROBE_API_KEY", ""),
        help="供应商 API Key；缺省读环境变量 METASO_PROBE_API_KEY。不落盘、不打印。",
    )
    parser.add_argument("--first-frame-url", default=DEFAULT_FIRST_FRAME_URL)
    parser.add_argument("--skip-first-frame", action="store_true", help="只跑文生探针")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求体，不发出调用")
    args = parser.parse_args()

    key = args.api_key.strip()
    if not key:
        print("缺少 API Key：请设置环境变量 METASO_PROBE_API_KEY 或传 --api-key。", file=sys.stderr)
        return 2
    provider = MetasoH3Provider(api_key=key, transport=UrllibMetasoHttpTransport())

    outcomes = [
        run_probe(
            provider, name="文生 16 秒", duration=16, first_frame_url=None, dry_run=args.dry_run
        )
    ]
    if not args.skip_first_frame:
        outcomes.append(
            run_probe(
                provider,
                name="首帧图生 30 秒",
                duration=30,
                first_frame_url=args.first_frame_url,
                dry_run=args.dry_run,
            )
        )
    for outcome in outcomes:
        outcome.report()

    accepted = [o for o in outcomes if o.verdict.startswith("ACCEPTED")]
    rejected = [o for o in outcomes if o.verdict.startswith("REJECTED")]
    print("\n===== 探针结论 =====")
    if args.dry_run:
        print("（dry-run：未发出任何调用；去掉 --dry-run 并配置真实 Key 后执行实测。）")
        return 0
    if not args.skip_first_frame:
        t2v = outcomes[0]
        i2v = outcomes[1]
        t2v_open = "放行" if t2v.verdict.startswith("ACCEPTED") else "未放行/不确定"
        i2v_ok = i2v.verdict.startswith("ACCEPTED")
        i2v_open = "放行" if i2v_ok else "未放行/不确定"
        i2v_gate = "" if i2v_ok else "（需在前端加「30 秒暂不支持首帧图生」门禁）"
        print(f"文生 16-30 秒：{t2v_open}")
        print(f"首帧图生 30 秒：{i2v_open}{i2v_gate}")
    if rejected and not accepted:
        print(
            "结论：v2 端点未放行长档——停止上线，退路为自建 2 段链"
            "（见 docs/长视频生成方案研究-2026-09-22.md）。"
        )
        return 1
    if any(o.verdict.startswith("UNCERTAIN") for o in outcomes):
        print("结论：存在不确定项——先到供应商控制台核对是否有残留计费任务再重试。")
        return 3
    print("结论：长档放行——可按文档执行后台启档与前端放开。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
