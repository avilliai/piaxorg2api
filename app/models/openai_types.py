"""
OpenAI 兼容的请求 / 响应 Pydantic 模型。
"""

from typing import Any, Literal
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class ContentPartText(BaseModel):
    type: Literal["text"]
    text: str


class ContentPartImageUrl(BaseModel):
    type: Literal["image_url"]
    image_url: dict[str, str]


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool", "function"]
    content: str | list[ContentPartText | ContentPartImageUrl] | None = None
    name: str | None = None
    tool_call_id: str | None = None

    def to_text(self) -> str:
        """将 content（可能是字符串或块列表）统一转为纯文本。"""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            return "\n".join(
                part.text for part in self.content if isinstance(part, ContentPartText)
            )
        return ""


class ChatCompletionRequest(BaseModel):
    model: str = "gpt-5"
    messages: list[Message]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: str | list[str] | None = None
    user: str | None = None
    # 工具调用（透传字段，暂不执行）
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict | None = None

    def build_prompt(self) -> str:
        """
        将多轮消息拼成单条 text。
        规则：system 消息作为前缀；其余角色逐条拼接。
        """
        parts: list[str] = []
        for msg in self.messages:
            text = msg.to_text()
            if not text:
                continue
            if msg.role == "system":
                parts.insert(0, f"[系统指令]\n{text}\n")
            elif msg.role == "user":
                parts.append(f"User: {text}")
            elif msg.role == "assistant":
                parts.append(f"Assistant: {text}")
            else:
                parts.append(text)
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# 响应模型（供文档 / 类型检查用，实际返回 dict 更灵活）
# ---------------------------------------------------------------------------

class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChoiceMessage(BaseModel):
    role: str
    content: str


class Choice(BaseModel):
    index: int
    message: ChoiceMessage
    finish_reason: str | None = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


# ---------------------------------------------------------------------------
# 模型列表响应
# ---------------------------------------------------------------------------

class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "arting"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelInfo]
