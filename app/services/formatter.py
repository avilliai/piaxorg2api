"""
OpenAI 格式转换器：将 arting.ai 的 SSE 原始响应转化为标准 OpenAI Chat Completions 格式。
"""

import json
import time
import uuid
import logging
from typing import AsyncGenerator

logger = logging.getLogger(__name__)


def _make_chunk_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _parse_arting_line(raw_line: str) -> str | None:
    """
    解析 arting SSE 行，提取文字增量。

    arting.ai 实际返回两种格式：
      1. 标准 SSE:  data: {"type":"text","content":"..."}
      2. 纯文本行:  I'm sorry, ...  （无 data: 前缀，直接是内容）
    """
    line = raw_line.strip()
    if not line:
        return None

    # ── 标准 SSE 行 ──────────────────────────────────────
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload in ("[DONE]", ""):
            return None
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            # data: 后跟纯文本（非 JSON）
            return payload or None

        # Piax/OpenRouter-compatible chunks nest text under choices[].delta.
        choices = obj.get("choices") or []
        if choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta") or {}
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str) and content:
                    return content
            if choice.get("finish_reason"):
                return None

        t = obj.get("type", "")
        if t == "done" or obj.get("done"):
            return None

        for key in ("content", "text", "delta", "message"):
            val = obj.get(key)
            if val and isinstance(val, str):
                return val

        return None

    # ── 纯文本行（无 data: 前缀）────────────────────────
    # arting.ai 有时直接返回裸文本，例如错误提示或正文内容
    # 跳过明显的 SSE 控制行
    if line.startswith(("event:", "id:", "retry:", ":")):
        return None

    return line  # 直接当作内容返回


# ---------------------------------------------------------------------------
# 流式格式生成
# ---------------------------------------------------------------------------

async def to_openai_stream(
    upstream_gen: AsyncGenerator[str, None],
    model: str,
    chunk_id: str | None = None,
) -> AsyncGenerator[str, None]:
    chunk_id = chunk_id or _make_chunk_id()
    created = int(time.time())
    sent_role = False

    async for raw_line in upstream_gen:
        delta_text = _parse_arting_line(raw_line)

        if not sent_role:
            role_chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"
            sent_role = True

        if delta_text is None:
            continue

        chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": delta_text},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    stop_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 非流式格式
# ---------------------------------------------------------------------------

async def to_openai_json(
    upstream_gen: AsyncGenerator[str, None],
    model: str,
) -> dict:
    parts: list[str] = []
    async for raw_line in upstream_gen:
        delta = _parse_arting_line(raw_line)
        if delta:
            parts.append(delta)

    full_content = "".join(parts)
    completion_id = _make_chunk_id()
    created = int(time.time())
    completion_tokens = len(full_content)

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": full_content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": completion_tokens,
            "total_tokens": completion_tokens,
        },
    }
