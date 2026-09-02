"""
Anthropic Messages API 兼容的请求 / 响应 Pydantic 模型。

⚠️ 全新文件，不修改 app/models/openai_types.py 中的任何内容。
放置路径：app/models/anthropic_types.py
"""

import json
from typing import Any
from pydantic import BaseModel, Field


class AnthropicTool(BaseModel):
    name: str
    description: str | None = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class AnthropicMessage(BaseModel):
    # 官方规范里 messages 只应有 user/assistant，但 Claude Code CLI 等客户端
    # 实际发送时偶尔会把 system 提醒也塞进 messages 数组里（而不仅是顶层 system 字段）。
    # 这里放宽成 str，交给 to_internal_messages 里的下游逻辑处理，
    # 避免因为角色名不在白名单里就直接 422。
    role: str
    content: str | list[dict[str, Any]]


class AnthropicRequest(BaseModel):
    model: str
    messages: list[AnthropicMessage]
    system: str | list[dict[str, Any]] | None = None
    max_tokens: int = 4096
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stream: bool = False
    stop_sequences: list[str] | None = None
    tools: list[AnthropicTool] | None = None
    tool_choice: dict[str, Any] | str | None = None
    metadata: dict[str, Any] | None = None

    # -----------------------------------------------------------------
    def system_text(self) -> str:
        """Anthropic 的 system 是顶层字段（字符串或 block 列表），这里统一转成纯文本。"""
        if self.system is None:
            return ""
        if isinstance(self.system, str):
            return self.system
        parts = []
        for block in self.system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)

    def normalized_tools(self) -> list[dict[str, Any]]:
        """
        把 Anthropic 的 tool 定义（name/description/input_schema）
        转成 chat.py 里 _build_tool_system_prompt / _validate_tool_call 认识的形状
        （name/description/parameters），这样可以直接复用现成的伪工具调用机制。
        """
        if not self.tools:
            return []
        result = []
        for t in self.tools:
            result.append({
                "name": t.name,
                "description": t.description or "",
                "parameters": t.input_schema or {"type": "object", "properties": {}},
            })
        return result

    def to_internal_messages(self) -> list[dict[str, Any]]:
        """
        把 Anthropic messages 转换为项目内部通用格式：
            [{"role": "user"/"assistant", "content": str | list[{"type":"text"/"image_url",...}]}]
        与 chat.py 处理 OpenAI 请求后得到的格式保持一致，方便复用
        _extract_and_filter_images / 工具调用解析等逻辑，不需要为 Anthropic 单独写一套。
        """
        result: list[dict[str, Any]] = []

        for msg in self.messages:
            role = msg.role
            content = msg.content

            if isinstance(content, str):
                result.append({"role": role, "content": content})
                continue

            blocks: list[dict[str, Any]] = []
            tool_result_texts: list[str] = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "text":
                    blocks.append({"type": "text", "text": block.get("text", "")})

                elif btype == "image":
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        media_type = source.get("media_type", "image/png")
                        data = source.get("data", "")
                        blocks.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{data}"},
                        })
                    elif source.get("type") == "url":
                        blocks.append({
                            "type": "image_url",
                            "image_url": {"url": source.get("url", "")},
                        })

                elif btype == "tool_use":
                    name = block.get("name", "")
                    tool_input = block.get("input", {})
                    blocks.append({
                        "type": "text",
                        "text": f"[之前调用了工具 {name}，参数: {json.dumps(tool_input, ensure_ascii=False)}]",
                    })

                elif btype == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    raw = block.get("content", "")
                    if isinstance(raw, list):
                        texts = [b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text"]
                        raw_text = "\n".join(texts)
                    else:
                        raw_text = str(raw)
                    tool_result_texts.append(f"[工具结果 {tool_use_id}]\n{raw_text}")

            if tool_result_texts:
                merged = "\n\n".join(tool_result_texts)
                if blocks:
                    blocks.append({"type": "text", "text": merged})
                    result.append({"role": role, "content": blocks})
                else:
                    result.append({
                        "role": "user",
                        "content": f"{merged}\n请基于以上工具结果直接回复用户，不要重复调用同一个工具",
                    })
                continue

            if blocks:
                if all(b["type"] == "text" for b in blocks):
                    result.append({"role": role, "content": "\n".join(b["text"] for b in blocks)})
                else:
                    result.append({"role": role, "content": blocks})

        return result