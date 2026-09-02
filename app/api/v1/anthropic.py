"""
Anthropic Messages API 兼容层：POST /v1/messages

⚠️ 全新文件，不修改 app/api/v1/chat.py 中的任何代码。
以只读方式导入 chat.py 里已经调试好的工具调用解析 / 图片处理 / 重试逻辑，
复用同一套机制，保证行为与 OpenAI 端点一致，同时不影响原有端点。

放置路径：app/api/v1/anthropic.py
需要在 app/app.py 里追加一行注册（见随附的修改版 app.py）。
"""

import asyncio
import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from dotenv import load_dotenv

from app.models.anthropic_types import AnthropicRequest
from app.services.token_manager import TokenManager
from app.services.arting_client import stream_chat, upload_file_to_arting
from app.services.formatter import _parse_arting_line
from app.services.anthropic_formatter import (
    to_anthropic_stream,
    to_anthropic_json,
    build_plain_response,
    build_plain_stream,
    build_tool_use_response,
    build_tool_use_stream,
)
from app.core.dependencies import get_token_manager

# ── 只读复用 chat.py 中已经调试好的工具调用 / 图片处理逻辑，不做任何修改 ──
from app.api.v1.chat import (
    _extract_and_filter_images,
    _build_tool_system_prompt,
    _check_truncation,
    _parse_tool_calls,
    _validate_tool_call,
    calc_backoff,
    MAX_RETRIES,
)

load_dotenv()
logger = logging.getLogger(__name__)
router = APIRouter()


async def _report_upstream_failure(token_manager: TokenManager, auth_token: str, exc: Exception):
    if "100005" in str(exc):
        await token_manager.recover_auth_failure(auth_token)
    else:
        await token_manager.report_request_failure(auth_token, str(exc))

API_KEY = os.getenv("API_KEY", "")


async def verify_anthropic_api_key(
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    authorization: str | None = Header(default=None),
):
    """
    Claude 官方客户端（含 Claude Code CLI）默认发 `x-api-key` 头，
    这里额外兼容 `Authorization: Bearer`，方便某些自定义客户端。
    鉴权逻辑与 app/core/auth.py 里的 verify_api_key 保持一致
    （同一个 API_KEY 环境变量，未设置则开发模式放行），但不修改原文件，
    避免影响 OpenAI 端点的鉴权行为。
    """
    if not API_KEY:
        return
    provided = x_api_key
    if not provided and authorization:
        provided = authorization.removeprefix("Bearer ").strip()
    if provided != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key. Set x-api-key: <your_api_key>",
        )


async def _collect_raw_text(upstream_gen) -> str:
    parts = []
    async for raw_line in upstream_gen:
        delta = _parse_arting_line(raw_line)
        if delta:
            parts.append(delta)
    return "".join(parts)


def _estimate_input_tokens(system_text: str, messages: list[dict]) -> int:
    total = len(system_text)
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            total += sum(len(b.get("text", "")) for b in c if isinstance(b, dict))
    return total


@router.post(
    "/messages",
    summary="Messages (Anthropic Compatible)",
    dependencies=[Depends(verify_anthropic_api_key)],
)
async def anthropic_messages(
    req: AnthropicRequest,
    token_manager: TokenManager = Depends(get_token_manager),
):
    has_tools = bool(req.tools)
    current_tools = req.normalized_tools() if has_tools else []

    base_messages = req.to_internal_messages()
    system_text = req.system_text()
    if system_text:
        base_messages.insert(0, {"role": "system", "content": system_text})

    base_messages, raw_images = await _extract_and_filter_images(base_messages)
    input_tokens_estimate = _estimate_input_tokens(system_text, base_messages)

    logger.info(
        "anthropic/messages | model=%s stream=%s msg_count=%d has_tools=%s image_count=%d",
        req.model, req.stream, len(base_messages), has_tools, len(raw_images),
    )

    # ════════════════════════════════════════════
    # 流式
    # ════════════════════════════════════════════
    if req.stream:
        async def sse_generator():
            last_error: Exception = RuntimeError("未知错误")
            tools_this_round = current_tools

            for attempt in range(1, MAX_RETRIES + 1):
                auth_token = await token_manager.get_next()
                attempt_messages = list(base_messages)

                if has_tools and tools_this_round:
                    tool_system = _build_tool_system_prompt(tools_this_round)
                    insert_pos = next((i + 1 for i, m in enumerate(attempt_messages) if m["role"] == "system"), 0)
                    attempt_messages.insert(insert_pos, {"role": "system", "content": tool_system})

                arting_files = []
                if raw_images:
                    try:
                        upload_tasks = [upload_file_to_arting(auth_token, img) for img in raw_images]
                        upload_results = await asyncio.gather(*upload_tasks)
                        arting_files = [res for res in upload_results if res]
                    except Exception as e:
                        logger.warning("anthropic stream attempt %d 图片上传失败: %s", attempt, e)

                upstream_completed = False
                try:
                    if has_tools:
                        upstream = stream_chat(auth_token=auth_token, messages=attempt_messages,
                                                model=req.model, files=arting_files)
                        full_text = await _collect_raw_text(upstream)
                        upstream_completed = True
                        await token_manager.report_request_success(auth_token)

                        is_truncated, fallback_text, next_tools = _check_truncation(full_text, current_tools)
                        if is_truncated:
                            if attempt < MAX_RETRIES:
                                tools_this_round = next_tools
                                logger.warning("anthropic stream(tools) attempt %d 被截断，过滤 tools 重试", attempt)
                                raise RuntimeError("Tool call unexpectedly truncated")
                            else:
                                yield build_plain_stream(fallback_text, req.model, input_tokens_estimate)
                                return

                        clean_text, tool_calls = _parse_tool_calls(full_text)

                        if tool_calls:
                            invalid_calls = []
                            for tc in tool_calls:
                                valid, missing = _validate_tool_call(tc, current_tools)
                                if not valid:
                                    invalid_calls.append((tc["name"], missing))
                            if invalid_calls:
                                if attempt < MAX_RETRIES:
                                    logger.warning("anthropic stream(tools) attempt %d 参数不完整 %s，重试",
                                                   attempt, invalid_calls)
                                    raise RuntimeError(f"Tool call missing required arguments: {invalid_calls}")
                                else:
                                    yield build_plain_stream(clean_text or full_text, req.model, input_tokens_estimate)
                                    return

                        if tool_calls:
                            yield build_tool_use_stream(tool_calls, clean_text, req.model, input_tokens_estimate)
                        else:
                            yield build_plain_stream(clean_text or full_text, req.model, input_tokens_estimate)
                        return

                    # 无工具：真正的增量流式
                    upstream = stream_chat(auth_token=auth_token, messages=attempt_messages,
                                            model=req.model, files=arting_files)
                    chunks: list[str] = []
                    async for piece in to_anthropic_stream(upstream, req.model, input_tokens_estimate):
                        chunks.append(piece)
                    upstream_completed = True
                    await token_manager.report_request_success(auth_token)
                    for piece in chunks:
                        yield piece
                    return

                except Exception as e:
                    if not upstream_completed:
                        await _report_upstream_failure(token_manager, auth_token, e)
                    last_error = e
                    logger.warning("anthropic stream attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(calc_backoff(attempt))

            logger.error("anthropic stream all %d attempts failed: %s", MAX_RETRIES, last_error)
            err_msg = str(last_error).replace('"', "'")
            yield f'event: error\ndata: {{"type":"error","error":{{"type":"api_error","message":"{err_msg}"}}}}\n\n'

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ════════════════════════════════════════════
    # 非流式
    # ════════════════════════════════════════════
    last_error: Exception = RuntimeError("未知错误")
    tools_this_round = current_tools

    for attempt in range(1, MAX_RETRIES + 1):
        auth_token = await token_manager.get_next()
        attempt_messages = list(base_messages)

        if has_tools and tools_this_round:
            tool_system = _build_tool_system_prompt(tools_this_round)
            insert_pos = next((i + 1 for i, m in enumerate(attempt_messages) if m["role"] == "system"), 0)
            attempt_messages.insert(insert_pos, {"role": "system", "content": tool_system})

        arting_files = []
        if raw_images:
            try:
                upload_tasks = [upload_file_to_arting(auth_token, img) for img in raw_images]
                upload_results = await asyncio.gather(*upload_tasks)
                arting_files = [res for res in upload_results if res]
            except Exception as e:
                logger.warning("anthropic non-stream attempt %d 图片上传失败: %s", attempt, e)

        upstream = stream_chat(auth_token=auth_token, messages=attempt_messages, model=req.model, files=arting_files)

        upstream_completed = False
        try:
            if has_tools:
                full_text = await _collect_raw_text(upstream)
                upstream_completed = True
                await token_manager.report_request_success(auth_token)

                is_truncated, fallback_text, next_tools = _check_truncation(full_text, current_tools)
                if is_truncated:
                    if attempt < MAX_RETRIES:
                        tools_this_round = next_tools
                        logger.warning("anthropic non-stream(tools) attempt %d 被截断，过滤 tools 重试", attempt)
                        raise RuntimeError("Tool call unexpectedly truncated")
                    else:
                        return JSONResponse(content=build_plain_response(fallback_text, req.model, input_tokens_estimate))

                clean_text, tool_calls = _parse_tool_calls(full_text)

                if tool_calls:
                    invalid_calls = []
                    for tc in tool_calls:
                        valid, missing = _validate_tool_call(tc, current_tools)
                        if not valid:
                            invalid_calls.append((tc["name"], missing))
                    if invalid_calls:
                        if attempt < MAX_RETRIES:
                            logger.warning("anthropic non-stream(tools) attempt %d 参数不完整 %s，重试",
                                           attempt, invalid_calls)
                            raise RuntimeError(f"Tool call missing required arguments: {invalid_calls}")
                        else:
                            return JSONResponse(content=build_plain_response(clean_text or full_text, req.model,
                                                                              input_tokens_estimate))

                if tool_calls:
                    return JSONResponse(content=build_tool_use_response(tool_calls, clean_text, req.model,
                                                                         input_tokens_estimate))
                else:
                    return JSONResponse(content=build_plain_response(clean_text or full_text, req.model,
                                                                      input_tokens_estimate))
            else:
                result = await to_anthropic_json(upstream, req.model, input_tokens_estimate)
                upstream_completed = True
                await token_manager.report_request_success(auth_token)
                return JSONResponse(content=result)

        except Exception as e:
            if not upstream_completed:
                await _report_upstream_failure(token_manager, auth_token, e)
            last_error = e
            logger.warning("anthropic non-stream attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(calc_backoff(attempt))

    logger.error("anthropic non-stream all %d attempts failed: %s", MAX_RETRIES, last_error)
    return JSONResponse(
        status_code=502,
        content={"type": "error", "error": {"type": "api_error", "message": str(last_error)}},
    )


@router.post(
    "/messages/count_tokens",
    summary="Count Tokens (rough estimate)",
    dependencies=[Depends(verify_anthropic_api_key)],
)
async def count_tokens(req: AnthropicRequest):
    """
    Claude Code CLI 在某些场景会调用这个端点做粗略计费预估。
    上游 arting.ai 没有真正的 tokenizer，这里用字符数做保守估算，
    只是为了让客户端不报错，不代表真实计费。
    """
    base_messages = req.to_internal_messages()
    system_text = req.system_text()
    total = _estimate_input_tokens(system_text, base_messages)
    return {"input_tokens": max(1, total // 2)}
