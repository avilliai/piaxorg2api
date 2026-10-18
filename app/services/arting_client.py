import json, uuid, re
import httpx

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

REAL_MODELS = list(MODEL_AGENT.keys())
AVAILABLE_MODELS = [{"id": f"piax/{m}", "object": "model", "owned_by": "piax"} for m in REAL_MODELS]


async def stream_chat(auth_token: str, messages: list[dict], model: str = "agnes-2.5-flash", files=None):
    conversation_id = str(uuid.uuid4())
    model_name = model.removeprefix("piax/")

    if model_name not in MODEL_AGENT:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as catalog_client:
                page = (await catalog_client.get("https://www.piax.org/home")).text
            for mid, name, path in re.findall(
                r'\\"id\\":\\"(\d+)\\".*?\\"name\\":\\"([^\\"]+)\\".*?\\"path\\":\\"([^\\"]+)\\"', page
            ):
                MODEL_AGENT[path] = mid
                if path == model_name or name.lower() == model_name.lower():
                    MODEL_AGENT[model_name] = mid
        except Exception:
            pass

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
    agent_id = MODEL_AGENT.get(model_name) or "422"

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
        "accept": "text/event-stream, application/json",
        "content-type": "application/json",
        "authorization": auth_token,
        "origin": "https://www.piax.org",
        "referer": "https://www.piax.org/",
    }

    async def _do_stream(aid: str):
        p = {**payload, "agentId": aid}
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("POST", BASE + CHAT_PATH, headers=headers, json=p) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:") and '"event":"error"' in line:
                        try:
                            err_obj = json.loads(line[5:].strip())
                            ans = err_obj.get("answer") or err_obj.get("message") or "Piax upstream error"
                            raise RuntimeError(f"Piax upstream error ({err_obj.get('code', 500)}): {ans}")
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
    headers = {"authorization": token, "content-type": "application/json", "origin": "https://www.piax.org", "referer": "https://www.piax.org/"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            BASE + "/trade-api/trade/daily/status",
            headers=headers,
            json={"device": {"deviceType": "pc", "osPlatform": "web"}},
        )
        return r.json()


async def claim_daily(token: str) -> dict:
    headers = {"authorization": token, "content-type": "application/json", "origin": "https://www.piax.org", "referer": "https://www.piax.org/"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            BASE + "/trade-api/trade/daily/claim",
            headers=headers,
            json={"device": {"deviceType": "pc", "osPlatform": "web"}},
        )
        return r.json()


async def get_balance(token: str) -> dict:
    headers = {"authorization": token, "content-type": "application/json", "origin": "https://www.piax.org", "referer": "https://www.piax.org/"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            BASE + "/trade-api/trade/subscription/getUserSubscriptionStatus",
            headers=headers,
            json={"productNo": "pia", "device": {"deviceType": "pc", "osPlatform": "web"}},
        )
        return r.json()
