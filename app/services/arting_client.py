import json, uuid, re, logging
import httpx

logger = logging.getLogger(__name__)

BASE = "https://piax-api.piax.org"
CHAT_PATH = "/ai-api/ai/agent/chatStream"

MODEL_AGENT = {
    "agnes-2.5-flash": "422",
    "agnes-3-flash": "421",
    "deepseek-v4-1-flash": "212",
    "gpt-6-astra": "210",
    "glm-5-3-flash": "203",
    "gemini-4-argon": "453",
    "claude-sonnet-5-5": "427",
    "claude-opus-5-5": "418",
    "grok-4-7": "415",
    "gpt-6-1-sol": "429",
    "gpt-6-sol": "420",
    "gemini-3-7-flash": "199",
    "claude-opus-5": "188",
    "gpt5-6-sol": "185",
    "kimi-k-3": "186",
    "deepseek-v4-pro": "168",
    "perplexity": "24",
    "gpt5-6-terra": "184",
    "gpt5-6-luna": "183",
    "gpt5-5-pro": "166",
    "gpt5-5": "165",
    "gpt5-4": "145",
    "gpt5-3": "144",
    "gpt5-3-codex": "141",
    "gpt5-2": "123",
    "gpt-5-1": "101",
    "gpt-5": "90",
    "gpt-4o": "7",
    "gpt-4-1": "31",
    "gpt-4o-mini": "6",
    "o3": "66",
    "o4-mini": "40",
    "o1": "46",
    "claude-sonnet-5": "180",
    "claude-opus-4-5": "119",
    "claude-sonnet-4-6": "139",
    "gemini-3-6-flash": "187",
    "gemini-3-5-flash": "172",
    "gemini-3-1-pro": "140",
    "gemini-2-5-pro": "37",
    "gemini-2-5-flash": "38",
    "deepseek-v4-flash": "167",
    "deepseek-v32": "120",
    "grok-4-6": "197",
    "grok-4-5": "182",
    "grok-4-3": "170",
    "glm-5-3": "200",
    "glm-5-2": "178",
    "qwen3-8-max": "201",
    "qwen3-7-max": "173",
    "qwen3-6-plus": "161",
    "qwen3-coder": "84",
    "kimi-k2-6": "163",
    "kimi-k2-5": "132",
    "minimax-m3": "175",
    "minimax-m2-7": "150",
    "mistral-large-3-2512": "121",
}

# 扩展别名映射，支持用户常用的命名变体（如点号、横线、无横线等）
ALIAS_MAP = {
    "gpt-5.6-luna": "183",
    "gpt-5-6-luna": "183",
    "gpt5.6-luna": "183",
    "gpt-5.6-terra": "184",
    "gpt-5-6-terra": "184",
    "gpt-5.6-sol": "185",
    "gpt-5-6-sol": "185",
    "gpt-5.5": "165",
    "gpt-5.5-pro": "166",
    "claude-sonnet-5.5": "427",
    "claude-opus-5.5": "418",
    "claude-sonnet-4.6": "139",
    "claude-opus-4.5": "119",
    "gemini-3.7-flash": "199",
    "gemini-3.6-flash": "187",
    "gemini-3.5-flash": "172",
    "gemini-2.5-pro": "37",
    "gemini-2.5-flash": "38",
    "glm-5.3-flash": "203",
    "glm-5.3": "200",
    "glm-5.2": "178",
    "grok-4.7": "415",
    "grok-4.6": "197",
    "grok-4.5": "182",
    "kimi-k.3": "186",
    "kimi-k-3": "186",
    "kimi-k2.6": "163",
    "kimi-k2.5": "132",
    "qwen3.8-max": "201",
    "qwen3.7-max": "173",
    "qwen3.6-plus": "161",
    "minimax-m2.7": "150",
    "agnes": "422",
}
MODEL_AGENT.update(ALIAS_MAP)

REAL_MODELS = list(MODEL_AGENT.keys())
AVAILABLE_MODELS = [{"id": f"piax/{m}", "object": "model", "owned_by": "piax"} for m in REAL_MODELS]


def resolve_agent_id(model_name: str) -> str:
    cleaned = model_name.removeprefix("piax/").strip().lower()
    if cleaned in MODEL_AGENT:
        return MODEL_AGENT[cleaned]
    # 替换点为短横线再查
    normalized = cleaned.replace(".", "-")
    if normalized in MODEL_AGENT:
        return MODEL_AGENT[normalized]
    # 去除所有点和短横线再模糊匹配
    clean_alnum = re.sub(r"[^a-z0-9]", "", cleaned)
    for k, v in MODEL_AGENT.items():
        if re.sub(r"[^a-z0-9]", "", k) == clean_alnum:
            return v
    return "422"


async def stream_chat(auth_token: str, messages: list[dict], model: str = "agnes-2.5-flash", files=None):
    conversation_id = str(uuid.uuid4())
    model_name = model.removeprefix("piax/")
    agent_id = resolve_agent_id(model_name)

    history = []
    for m in messages:
        role, content = m.get("role", "user"), m.get("content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif item.get("type") in ("image_url", "image"):
                        parts.append(f"[image: {item.get('image_url', item.get('url', ''))}]")
            content = "\n".join(parts)
        history.append(f"{role}: {content}")
    query = "\n".join(history)
    files = files or []

    payload = {
        "device": {"deviceType": "pc", "osPlatform": "web"},
        "agentId": agent_id,
        "query": query,
        "conversationId": conversation_id,
        "parentMessageId": "",
        "files": files,
        "inputs": {"tools.enable": "false"},
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "accept": "text/event-stream, application/json",
        "content-type": "application/json",
        "authorization": auth_token,
        "origin": "https://www.piax.org",
        "referer": "https://www.piax.org/",
    }

    async def _do_stream(aid: str):
        p = {**payload, "agentId": aid}
        timeout = httpx.Timeout(connect=15.0, read=90.0, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("POST", BASE + CHAT_PATH, headers=headers, json=p) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    # 严格捕获 upstream 错误事件：error / bill_message
                    if line.startswith("data:"):
                        payload_str = line[5:].strip()
                        if payload_str == "error":
                            raise RuntimeError("Piax upstream error: model busy / error")
                        if '"event":"error"' in line or '"event":"bill_message"' in line:
                            try:
                                err_obj = json.loads(payload_str)
                                code = err_obj.get("code", 500)
                                ans = err_obj.get("answer") or err_obj.get("message") or ""
                                event_type = err_obj.get("event")
                                if event_type == "bill_message" or code == 401 or "balance" in ans.lower() or "insufficient" in ans.lower():
                                    raise RuntimeError(f"Piax quota exhausted (code {code}): {ans or 'insufficient balance'}")
                                raise RuntimeError(f"Piax upstream error ({code}): {ans or event_type}")
                            except json.JSONDecodeError:
                                pass
                    yield line if line.startswith("data:") else "data: " + line

    try:
        async for line in _do_stream(agent_id):
            yield line
    except RuntimeError as e:
        err_msg = str(e)
        if ("does not support this feature" in err_msg or "subscription plan" in err_msg) and agent_id != "422":
            async for line in _do_stream("422"):
                yield line
        else:
            raise


async def upload_file_to_arting(auth_token: str, image_url_or_base64: str) -> dict:
    if not image_url_or_base64:
        return {}
    return {"url": image_url_or_base64, "type": "image"}


async def get_daily_status(token: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "authorization": token,
        "content-type": "application/json",
        "origin": "https://www.piax.org",
        "referer": "https://www.piax.org/"
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            BASE + "/trade-api/trade/daily/status",
            headers=headers,
            json={"device": {"deviceType": "pc", "osPlatform": "web"}},
        )
        return r.json()


async def claim_daily(token: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "authorization": token,
        "content-type": "application/json",
        "origin": "https://www.piax.org",
        "referer": "https://www.piax.org/"
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            BASE + "/trade-api/trade/daily/claim",
            headers=headers,
            json={"device": {"deviceType": "pc", "osPlatform": "web"}},
        )
        return r.json()


async def get_balance(token: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "authorization": token,
        "content-type": "application/json",
        "origin": "https://www.piax.org",
        "referer": "https://www.piax.org/"
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            BASE + "/trade-api/trade/subscription/getUserSubscriptionStatus",
            headers=headers,
            json={"productNo": "pia", "device": {"deviceType": "pc", "osPlatform": "web"}},
        )
        return r.json()
