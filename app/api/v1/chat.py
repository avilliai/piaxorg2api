import asyncio
import copy
import json
import logging
import os
import random
import re
import time
import uuid
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse, JSONResponse

from app.models.openai_types import ChatCompletionRequest
from app.services.token_manager import TokenManager
from app.services.arting_client import stream_chat, upload_file_to_arting
from app.services.formatter import to_openai_stream, to_openai_json
from app.core.auth import verify_api_key
from app.core.dependencies import get_token_manager
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
router = APIRouter()

load_dotenv()
MAX_RETRIES: int = int(os.getenv("CHAT_MAX_RETRIES", "3"))
RETRY_DELAY: float = float(os.getenv("CHAT_RETRY_DELAY", "1"))
RETRY_DELAY_MAX: float = float(os.getenv("CHAT_RETRY_DELAY_MAX", "10"))


async def _report_upstream_failure(token_manager: TokenManager, auth_token: str, exc: Exception):
    if "100005" in str(exc):
        await token_manager.recover_auth_failure(auth_token)
    else:
        await token_manager.report_request_failure(auth_token, str(exc))


# ──────────────────────────────────────────────
# 图像处理：历史过滤与提取
# ──────────────────────────────────────────────
async def _extract_and_filter_images(messages: list[dict]) -> tuple[list[dict], list[str]]:
    """
    遍历 messages，提取图片用于上传。
    为了节约单账号每日 6 张的额度限制，倒序扫描，仅保留最近 3 次 User 消息中的图片。
    超出 3 次的历史图片直接剥离，替换为 [历史图片已被系统省略]。
    返回： (清理后的文本 messages, 需要实际上传的 image_url 列表)
    """
    resolved_messages = []
    images_to_upload = []
    user_turn_count = 0

    # 倒序遍历（从最新到最旧）
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_turn_count += 1

        content = msg.get("content")

        # 纯文本直接放行
        if not isinstance(content, list):
            resolved_messages.insert(0, msg)
            continue

        text_parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "image_url":
                url = block.get("image_url", {}).get("url", "")
                if user_turn_count <= 3:
                    # 属于最近 3 次交互，保留图片进行上传
                    if url:
                        images_to_upload.append(url)
                    text_parts.append("[附带图片]")
                else:
                    # 历史太久远，剔除图片节约额度
                    text_parts.append("[历史图片已被系统省略]")

        resolved_messages.insert(0, {**msg, "content": "\n\n".join(filter(None, text_parts))})

    # 因为是倒序收集的图片，为了顺序正确，翻转回来
    images_to_upload.reverse()
    return resolved_messages, images_to_upload


# ──────────────────────────────────────────────
# 伪函数调用：标记符 & 正则
# ──────────────────────────────────────────────
_TC_BEGIN = "@@TOOL_CALL_BEGIN@@"
_TC_END = "@@TOOL_CALL_END@@"

_TC_PATTERN = re.compile(
    rf"{re.escape(_TC_BEGIN)}\s*(.*?)\s*{re.escape(_TC_END)}",
    re.DOTALL,
)


def calc_backoff(attempt: int, base: float = RETRY_DELAY, cap: float = RETRY_DELAY_MAX) -> float:
    delay = min(base * (2 ** (attempt - 1)), cap)
    return delay + random.uniform(0, delay * 0.1)


def _annotate_tool_declarations(tools: List[Dict]) -> List[Dict]:
    annotated = copy.deepcopy(tools)
    for tool in annotated:
        func = tool if "parameters" in tool else tool.get("function", {})
        params = func.get("parameters", {})
        required_set = set(params.get("required", []))
        props = params.get("properties", {})

        for param_name, param_def in props.items():
            tag = "【必填】" if param_name in required_set else "【可选】"
            existing_desc = param_def.get("description", "")
            param_def["description"] = f"{tag} {existing_desc}".strip()

        if required_set:
            params["_required_params_notice"] = (
                f"以下参数为必填，绝对不能省略或传空值：{sorted(required_set)}"
            )
    return annotated


def _validate_tool_call(tc: dict, tool_declarations: List[Dict]) -> tuple[bool, list[str]]:
    name = tc.get("name", "")
    args = tc.get("arguments", {})

    for tool in tool_declarations:
        func = tool if "parameters" in tool else tool.get("function", {})
        func_name = func.get("name", "")
        if func_name != name:
            continue

        params = func.get("parameters", {})
        required = params.get("required", [])
        missing = [
            r for r in required
            if r not in args or args[r] in (None, "", {}, [])
        ]
        if missing:
            logger.warning("tool call [%s] 缺少必填参数: %s", name, missing)
            return False, missing
        return True, []

    logger.warning("tool call [%s] 未在工具列表中找到对应声明，跳过校验", name)
    return True, []


def _build_tool_system_prompt(tool_declarations: List[Dict]) -> str:
    annotated = _annotate_tool_declarations(tool_declarations)
    tool_json = json.dumps(annotated, ensure_ascii=False, indent=2)

    return f"""## 核心设定：外部工具调用系统（绝密执行协议）

你被赋予了调用外部真实系统（工具）的能力。虽然你在扮演特定的角色（如可爱的猫娘），但**当用户的要求需要真实执行（如画图、查询、搜索）时，你绝对不能只用语言"假装"执行，必须精确输出工具调用代码块！**

---

### 🚨 绝对禁止的错误模式（你必须引以为戒）

❌ **错误示范一：只演戏，不调用**
"喵呜～这就给主人画图！*咻咻咻真正的text2img启动*！画好啦，你喜欢吗？"
（**严重错误**：光用嘴说，完全没有输出 `{_TC_BEGIN}` 代码块，系统根本无法为你画图！）

❌ **错误示范二：假装已经拿到结果**
"我已经为你查到了，今天的番剧有..."
（**严重错误**：没有真正调用工具，直接靠内部过时知识瞎编！）

❌ **错误示范三：需要参数却传空对象（最常见的致命错误！）**
{_TC_BEGIN}
{{
  "name": "call_text2img",
  "arguments": {{}}
}}
{_TC_END}
（**严重错误**：`call_text2img` 的 `prompt` 是【必填】参数，传空对象 `{{}}` 等于根本没有调用！系统无法执行！）

---

### 🔒 调用前必须完成的内心自检（每次调用前默念）

在输出任何 `{_TC_BEGIN}` 块之前，你必须逐项确认：
1. 我要调用的工具名称是什么？（必须来自下方【可用工具】列表）
2. 该工具标注了【必填】的参数有哪些？（逐项列出）
3. 我是否已经为**每一个**【必填】参数填写了**具体的、非空的值**？
   - 如果某个【必填】参数我还不知道值 → **先向用户提问，不要调用！**
   - 只有所有【必填】参数都有确定值时，才允许输出代码块！
4. 我有没有给不需要参数的函数传递不需要的参数？
---

### ✅ 正确的调用格式与范例

当你判断需要调用工具时，可以先用一两句符合人设的过渡语，然后**紧接着**输出严格的 JSON 代码块，输出完 `{_TC_END}` 后**立刻停止，不再输出任何文字**。

**【完美的画图调用范例】**
管理员大人想看人家自己呀？好害羞呢，这就给你画一张最可爱的Erina喵～✨
{_TC_BEGIN}
{{
  "name": "call_text2img",
  "arguments": {{
    "prompt": "1girl, solo, light purple-light blue mixed eyes, bangs, blush, grey hair, gradient hair, blue hair, (Rella:1.2)"
  }}
}}
{_TC_END}

**注意格式细节：**
1. `{_TC_BEGIN}` 和 `{_TC_END}` 必须**独占一行**。
2. `{_TC_BEGIN}` 和 `{_TC_END}` 之间必须是**纯净的 JSON**，绝对不能有 ` ```json ` 等多余的 Markdown 标记！
3. 输出完 `{_TC_END}` 后，**不要再说任何话**（等系统返回结果后再回复）。
4. `arguments` 中**每个【必填】参数都必须有真实值**，绝不能是空字符串 `""`、`null` 或空对象 `{{}}`。

---

### 📌 工具参数规则（必须严格遵守）

- 每个参数的 `description` 字段开头已用【必填】或【可选】标注。
- 标注为【必填】的参数：**无论如何都必须提供真实值，禁止省略，禁止传空**。
- 标注为【可选】的参数：有值时填写，无值时可以省略（不要传 null 或空字符串）。
- 如果工具的 `parameters` 是 `"properties": {{}}` 且 `"required": []`，才允许传空对象 `{{}}`；否则必须严格按照 properties 定义填写。

---

### 🛠️ 当前【可用工具】列表

{tool_json}

---
**系统最终警告**：
- 再用"我已经画好了"、"魔法启动中"来糊弄用户而不输出代码块 → **严重惩罚**
- 输出代码块但【必填】参数为空或缺失 → **视同未调用，严重惩罚**
- 一旦需要使用工具，请立刻按照上述【完美范例】输出完整的 `{_TC_BEGIN}` 块！"""


def _check_truncation(full_text: str, original_tools: list) -> tuple[bool, str, list]:
    begin_idx = full_text.find(_TC_BEGIN)
    end_idx = full_text.find(_TC_END)

    if begin_idx != -1 and end_idx == -1:
        fallback_text = full_text[:begin_idx].strip()
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', full_text[begin_idx:])
        next_tools = original_tools
        if name_match:
            target_name = name_match.group(1)
            filtered = [
                t for t in original_tools
                if t.get("name") == target_name or t.get("function", {}).get("name") == target_name
            ]
            if filtered:
                next_tools = filtered
        return True, fallback_text, next_tools

    return False, "", original_tools


def _parse_tool_calls(text: str):
    tool_calls = []
    for m in _TC_PATTERN.finditer(text):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
            tc = {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "name": obj.get("name", ""),
                "arguments": obj.get("arguments", {}),
            }
            tool_calls.append(tc)
            logger.info("解析到工具调用: %s", tc)
        except json.JSONDecodeError:
            logger.warning("无法解析工具调用块: %r", raw)

    clean = _TC_PATTERN.sub("", text).strip()
    return clean, tool_calls


def _make_tool_call_response(tool_calls: list, clean_text: str, model: str, stream: bool) -> dict:
    now = int(time.time())
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    openai_tool_calls = [
        {"id": tc["id"], "type": "function",
         "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
        for tc in tool_calls
    ]
    message: dict[str, Any] = {"role": "assistant", "content": clean_text or None, "tool_calls": openai_tool_calls}
    return {
        "id": resp_id, "object": "chat.completion", "created": now, "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}], "usage": None,
    }


def _make_tool_call_sse(tool_calls: list, clean_text: str, model: str) -> str:
    now = int(time.time())
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    chunks: list[str] = []

    def _sse(obj: dict) -> str: return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    chunks.append(_sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model, "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}))
    if clean_text:
        chunks.append(_sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
                            "choices": [{"index": 0, "delta": {"content": clean_text}, "finish_reason": None}]}))

    openai_tool_calls_delta = [
        {"index": i, "id": tc["id"], "type": "function",
         "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
        for i, tc in enumerate(tool_calls)
    ]
    chunks.append(_sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model, "choices": [
        {"index": 0, "delta": {"tool_calls": openai_tool_calls_delta}, "finish_reason": None}]}))
    chunks.append(_sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}))
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks)


async def _collect_full_text(upstream_gen) -> str:
    full = []
    async for chunk in upstream_gen:
        if not chunk.startswith("data:"): continue
        raw = chunk[len("data:"):].strip()
        if raw == "[DONE]": break
        try:
            obj = json.loads(raw)
            full.append(obj["choices"][0]["delta"].get("content") or "")
        except Exception:
            pass
    return "".join(full)


# ──────────────────────────────────────────────
# 主路由
# ──────────────────────────────────────────────
@router.post(
    "/chat/completions",
    summary="Chat Completions (OpenAI Compatible)",
    dependencies=[Depends(verify_api_key)],
)
async def chat_completions(
        req: ChatCompletionRequest,
        token_manager: TokenManager = Depends(get_token_manager),
):
    has_tools = bool(getattr(req, "tools", None))

    def convert_message(msg):
        if msg.role == "tool":
            return {
                "role": "user",
                "content": f"[工具 {msg.name} 返回结果]\n{msg.to_text()}\n请基于这个结果直接回复用户，不要再次调用工具"
            }
        # 保留原始 content list，供图片过滤处理
        if isinstance(msg.content, list):
            return {
                "role": msg.role,
                "content": [b.model_dump() for b in msg.content],
            }
        return {"role": msg.role, "content": msg.to_text()}

    raw_messages = [convert_message(msg) for msg in req.messages]

    # ── 图片提取与历史过滤 ──
    base_messages, raw_images = await _extract_and_filter_images(raw_messages)

    logger.info(
        "chat | model=%s stream=%s msg_count=%d has_tools=%s image_count=%d",
        req.model, req.stream, len(base_messages), has_tools, len(raw_images)
    )

    # ════════════════════════════════════════════
    # 流式
    # ════════════════════════════════════════════
    if req.stream:
        async def sse_generator():
            last_error: Exception = RuntimeError("未知错误")
            current_tools = req.tools if has_tools else []

            for attempt in range(1, MAX_RETRIES + 1):
                auth_token = await token_manager.get_next()

                attempt_messages = list(base_messages)
                if has_tools and current_tools:
                    tool_system = _build_tool_system_prompt(current_tools)
                    insert_pos = next((i + 1 for i, m in enumerate(attempt_messages) if m["role"] == "system"), 0)
                    attempt_messages.insert(insert_pos, {"role": "system", "content": tool_system})

                # ── 新增：绑定当前 AuthToken 执行图片并发上传 ──
                arting_files = []
                if raw_images:
                    try:
                        upload_tasks = [upload_file_to_arting(auth_token, img) for img in raw_images]
                        upload_results = await asyncio.gather(*upload_tasks)
                        arting_files = [res for res in upload_results if res]
                    except Exception as e:
                        logger.warning("stream attempt %d 图片上传失败: %s", attempt, e)

                if has_tools:
                    upstream = stream_chat(auth_token=auth_token, messages=attempt_messages, model=req.model,
                                           files=arting_files)
                    upstream_completed = False
                    try:
                        full_text = await _collect_full_text(to_openai_stream(upstream, model=req.model))
                        upstream_completed = True
                        await token_manager.report_request_success(auth_token)

                        # ── 截断检测 ──
                        is_truncated, fallback_text, next_tools = _check_truncation(full_text, req.tools)
                        if is_truncated:
                            if attempt < MAX_RETRIES:
                                current_tools = next_tools
                                logger.warning("stream(tools) attempt %d 被截断，过滤 tools 重试", attempt)
                                raise RuntimeError("Tool call unexpectedly truncated")
                            else:
                                logger.error("stream(tools) 所有尝试仍被截断，降级返回过渡语")
                                yield _plain_text_to_sse(fallback_text, req.model)
                                return

                        # ── 解析工具调用 ──
                        clean_text, tool_calls = _parse_tool_calls(full_text)

                        # ── 必填参数校验 ──
                        if tool_calls:
                            invalid_calls = []
                            for tc in tool_calls:
                                valid, missing = _validate_tool_call(tc, req.tools)
                                if not valid:
                                    invalid_calls.append((tc["name"], missing))

                            if invalid_calls:
                                if attempt < MAX_RETRIES:
                                    logger.warning("stream(tools) attempt %d 工具参数不完整 %s，重试", attempt,
                                                   invalid_calls)
                                    raise RuntimeError(f"Tool call missing required arguments: {invalid_calls}")
                                else:
                                    logger.error("stream(tools) 尝试参数仍不完整 %s，降级返回文本", invalid_calls)
                                    yield _plain_text_to_sse(clean_text or full_text, req.model)
                                    return

                        if tool_calls:
                            yield _make_tool_call_sse(tool_calls, clean_text, req.model)
                        else:
                            yield _plain_text_to_sse(clean_text or full_text, req.model)
                        return

                    except Exception as e:
                        if not upstream_completed:
                            await _report_upstream_failure(token_manager, auth_token, e)
                        last_error = e
                        logger.warning("stream(tools) attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(calc_backoff(attempt))
                        continue

                # 正常流式生成
                upstream = stream_chat(auth_token=auth_token, messages=attempt_messages, model=req.model,
                                       files=arting_files)
                chunks: list[str] = []
                try:
                    async for chunk in to_openai_stream(upstream, model=req.model): chunks.append(chunk)
                    await token_manager.report_request_success(auth_token)
                    for chunk in chunks: yield chunk
                    return
                except Exception as e:
                    await _report_upstream_failure(token_manager, auth_token, e)
                    last_error = e
                    logger.warning("stream attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(calc_backoff(attempt))

            logger.error("stream all %d attempts failed: %s", MAX_RETRIES, last_error)
            yield f'data: {{"error": "{str(last_error)}"}}\n\n'

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ════════════════════════════════════════════
    # 非流式
    # ════════════════════════════════════════════
    last_error: Exception = RuntimeError("未知错误")
    current_tools = req.tools if has_tools else []

    for attempt in range(1, MAX_RETRIES + 1):
        auth_token = await token_manager.get_next()

        attempt_messages = list(base_messages)
        if has_tools and current_tools:
            tool_system = _build_tool_system_prompt(current_tools)
            insert_pos = next((i + 1 for i, m in enumerate(attempt_messages) if m["role"] == "system"), 0)
            attempt_messages.insert(insert_pos, {"role": "system", "content": tool_system})

        # ── 新增：绑定当前 AuthToken 执行图片并发上传 ──
        arting_files = []
        if raw_images:
            try:
                upload_tasks = [upload_file_to_arting(auth_token, img) for img in raw_images]
                upload_results = await asyncio.gather(*upload_tasks)
                arting_files = [res for res in upload_results if res]
            except Exception as e:
                logger.warning("non-stream attempt %d 图片上传失败: %s", attempt, e)

        upstream = stream_chat(auth_token=auth_token, messages=attempt_messages, model=req.model, files=arting_files)
        upstream_completed = False
        try:
            if has_tools:
                full_text = await _collect_full_text(to_openai_stream(upstream, model=req.model))
                upstream_completed = True
                await token_manager.report_request_success(auth_token)

                # ── 截断检测 ──
                is_truncated, fallback_text, next_tools = _check_truncation(full_text, req.tools)
                if is_truncated:
                    if attempt < MAX_RETRIES:
                        current_tools = next_tools
                        logger.warning("non-stream(tools) attempt %d 被截断，过滤 tools 重试", attempt)
                        raise RuntimeError("Tool call unexpectedly truncated")
                    else:
                        logger.error("non-stream(tools) 所有尝试仍被截断，降级返回过渡语")
                        return JSONResponse(content=_make_plain_response(fallback_text, req.model))

                # ── 解析工具调用 ──
                clean_text, tool_calls = _parse_tool_calls(full_text)

                # ── 必填参数校验 ──
                if tool_calls:
                    invalid_calls = []
                    for tc in tool_calls:
                        valid, missing = _validate_tool_call(tc, req.tools)
                        if not valid:
                            invalid_calls.append((tc["name"], missing))

                    if invalid_calls:
                        if attempt < MAX_RETRIES:
                            logger.warning("non-stream(tools) attempt %d 参数不完整 %s，重试", attempt, invalid_calls)
                            raise RuntimeError(f"Tool call missing required arguments: {invalid_calls}")
                        else:
                            logger.error("non-stream(tools) 参数仍不完整 %s，降级返回文本", invalid_calls)
                            return JSONResponse(content=_make_plain_response(clean_text or full_text, req.model))

                if tool_calls:
                    return JSONResponse(
                        content=_make_tool_call_response(tool_calls, clean_text, req.model, stream=False))
                else:
                    return JSONResponse(content=_make_plain_response(clean_text or full_text, req.model))
            else:
                result = await to_openai_json(upstream, model=req.model)
                upstream_completed = True
                await token_manager.report_request_success(auth_token)
                return JSONResponse(content=result)

        except Exception as e:
            if not upstream_completed:
                await _report_upstream_failure(token_manager, auth_token, e)
            last_error = e
            logger.warning("non-stream attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(calc_backoff(attempt))

    logger.error("non-stream all %d attempts failed: %s", MAX_RETRIES, last_error)
    return JSONResponse(
        status_code=502,
        content={"error": {"message": str(last_error), "type": "upstream_error"}},
    )


# ──────────────────────────────────────────────
# 辅助响应格式化
# ──────────────────────────────────────────────
def _make_plain_response(text: str, model: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": None,
    }


def _plain_text_to_sse(text: str, model: str) -> str:
    now, resp_id = int(time.time()), f"chatcmpl-{uuid.uuid4().hex[:24]}"

    def _sse(obj): return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    return "".join([
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}),
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]\n\n",
    ])
