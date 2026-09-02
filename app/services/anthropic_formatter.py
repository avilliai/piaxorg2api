"""
Anthropic Messages API 格式转换器：把 arting.ai 的原始 SSE 响应
转换成 Anthropic /v1/messages 的流式事件 / 非流式响应格式。

⚠️ 全新文件，不修改 app/services/formatter.py。
仅以只读方式导入其中的 _parse_arting_line 做行解析（逻辑与 OpenAI 端点完全一致，
只是外层包装的事件结构不同）。
放置路径：app/services/anthropic_formatter.py
"""

import json
import uuid
import logging
from typing import AsyncGenerator, Any

from app.services.formatter import _parse_arting_line

logger = logging.getLogger(__name__)


def _make_msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _sse(event: str, data: dict) -> str:
    # Anthropic 的 SSE 格式要求带 event: 行，跟 OpenAI 纯 data: 行不同
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _rough_tokens(text: str) -> int:
    # 上游没有真正的 tokenizer，跟 formatter.py 里非流式 usage 估算方式保持一致的粗略估计
    return len(text)


# ---------------------------------------------------------------------------
# 纯文本流式（无工具调用，真正增量输出）
# ---------------------------------------------------------------------------
async def to_anthropic_stream(
    upstream_gen: AsyncGenerator[str, None],
    model: str,
    input_tokens: int = 0,
) -> AsyncGenerator[str, None]:
    msg_id = _make_msg_id()
    full_text_parts: list[str] = []

    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": model, "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    })
    yield _sse("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })

    async for raw_line in upstream_gen:
        delta_text = _parse_arting_line(raw_line)
        if not delta_text:
            continue
        full_text_parts.append(delta_text)
        yield _sse("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": delta_text},
        })

    yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})

    output_tokens = _rough_tokens("".join(full_text_parts))
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield _sse("message_stop", {"type": "message_stop"})


async def to_anthropic_json(
    upstream_gen: AsyncGenerator[str, None],
    model: str,
    input_tokens: int = 0,
) -> dict:
    parts: list[str] = []
    async for raw_line in upstream_gen:
        delta = _parse_arting_line(raw_line)
        if delta:
            parts.append(delta)
    return build_plain_response("".join(parts), model, input_tokens)


# ---------------------------------------------------------------------------
# 已收集好完整文本后的封装
# 对应 chat.py 里 _make_plain_response / _plain_text_to_sse 的使用场景：
# 截断兜底、工具参数校验失败降级等
# ---------------------------------------------------------------------------
def build_plain_response(text: str, model: str, input_tokens: int = 0) -> dict:
    msg_id = _make_msg_id()
    return {
        "id": msg_id, "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": _rough_tokens(text)},
    }


def build_plain_stream(text: str, model: str, input_tokens: int = 0) -> str:
    msg_id = _make_msg_id()
    chunks = [
        _sse("message_start", {
            "type": "message_start",
            "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [],
                        "model": model, "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": input_tokens, "output_tokens": 0}},
        }),
        _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                      "content_block": {"type": "text", "text": ""}}),
    ]
    if text:
        chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                     "delta": {"type": "text_delta", "text": text}}))
    chunks.append(_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
    chunks.append(_sse("message_delta", {"type": "message_delta",
                                          "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                          "usage": {"output_tokens": _rough_tokens(text)}}))
    chunks.append(_sse("message_stop", {"type": "message_stop"}))
    return "".join(chunks)


# ---------------------------------------------------------------------------
# 工具调用响应
# 对应 chat.py 的 _make_tool_call_response / _make_tool_call_sse，
# 只是把 OpenAI 的 function_call 形状换成 Anthropic 的 tool_use content block。
# 注意：跟 chat.py 现有实现一致，工具调用走的是"先收集完整文本再一次性输出"的方式
# （上游本身不支持真正的原生 function calling，这是伪工具调用机制的固有限制，
# 跟 OpenAI 端点的行为完全对齐，不是新引入的问题）。
# ---------------------------------------------------------------------------
def build_tool_use_response(tool_calls: list[dict], clean_text: str, model: str, input_tokens: int = 0) -> dict:
    msg_id = _make_msg_id()
    content: list[dict[str, Any]] = []
    if clean_text:
        content.append({"type": "text", "text": clean_text})
    for tc in tool_calls:
        content.append({
            "type": "tool_use",
            "id": tc["id"],
            "name": tc["name"],
            "input": tc.get("arguments", {}),
        })
    args_len = sum(len(json.dumps(tc.get("arguments", {}), ensure_ascii=False)) for tc in tool_calls)
    return {
        "id": msg_id, "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": _rough_tokens(clean_text) + args_len},
    }


def build_tool_use_stream(tool_calls: list[dict], clean_text: str, model: str, input_tokens: int = 0) -> str:
    msg_id = _make_msg_id()
    chunks = [_sse("message_start", {
        "type": "message_start",
        "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [],
                    "model": model, "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0}},
    })]

    idx = 0
    if clean_text:
        chunks.append(_sse("content_block_start", {"type": "content_block_start", "index": idx,
                                                     "content_block": {"type": "text", "text": ""}}))
        chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                                                     "delta": {"type": "text_delta", "text": clean_text}}))
        chunks.append(_sse("content_block_stop", {"type": "content_block_stop", "index": idx}))
        idx += 1

    for tc in tool_calls:
        chunks.append(_sse("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": {}},
        }))
        args_json = json.dumps(tc.get("arguments", {}), ensure_ascii=False)
        chunks.append(_sse("content_block_delta", {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "input_json_delta", "partial_json": args_json},
        }))
        chunks.append(_sse("content_block_stop", {"type": "content_block_stop", "index": idx}))
        idx += 1

    chunks.append(_sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": _rough_tokens(clean_text)},
    }))
    chunks.append(_sse("message_stop", {"type": "message_stop"}))
    return "".join(chunks)
