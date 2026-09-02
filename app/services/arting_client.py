import json, uuid, re
import httpx

BASE = "https://piax-api.piax.org"
# Piax chat messages are submitted through the conversation message endpoint.
CHAT_PATH = "/ai-api/ai/agent/chatStream"
MODEL_AGENT = {
 "glm-5-3-flash":"203","gemini-3-7-flash":"199","claude-opus-5":"188","gpt5-6-sol":"185","kimi-k-3":"186","deepseek-v4-pro":"168","perplexity":"24","gpt5-6-terra":"184","gpt5-6-luna":"183","gpt5-5-pro":"166","gpt5-5":"165","gpt5-4":"145","gpt5-3":"144","gpt5-3-codex":"141","gpt5-2":"123","gpt-5-1":"101","gpt-5":"90","gpt-4o":"7","gpt-4-1":"31","gpt-4o-mini":"6","o3":"66","o4-mini":"40","o1":"46","claude-sonnet-5":"180","claude-opus-4-5":"119","claude-sonnet-4-6":"139","gemini-3-6-flash":"187","gemini-3-5-flash":"172","gemini-3-1-pro":"140","gemini-2-5-pro":"37","gemini-2-5-flash":"38","deepseek-v4-flash":"167","deepseek-v32":"120","grok-4-6":"197","grok-4-5":"182","grok-4-3":"170","glm-5-3":"200","glm-5-2":"178","qwen3-8-max":"201","qwen3-7-max":"173","qwen3-6-plus":"161","qwen3-coder":"84","kimi-k2-6":"163","kimi-k2-5":"132","minimax-m3":"175","minimax-m2-7":"150","mistral-large-3-2512":"121"
}
REAL_MODELS = ["glm-5-3-flash","gemini-3-7-flash","claude-opus-5","gpt5-6-sol","kimi-k-3","deepseek-v4-pro","perplexity","gpt5-6-terra","gpt5-6-luna","gpt5-5-pro","gpt5-5","gpt5-4","gpt5-3","gpt5-3-codex","gpt5-2","gpt-5-1","gpt-5","gpt-4o","gpt-4-1","gpt-4o-mini","o3","o4-mini","o1","claude-sonnet-5","claude-opus-4-5","claude-sonnet-4-6","gemini-3-6-flash","gemini-3-5-flash","gemini-3-1-pro","gemini-2-5-pro","gemini-2-5-flash","deepseek-v4-flash","deepseek-v32","grok-4-6","grok-4-5","grok-4-3","glm-5-3","glm-5-2","qwen3-8-max","qwen3-7-max","qwen3-6-plus","qwen3-coder","kimi-k2-6","kimi-k2-5","minimax-m3","minimax-m2-7","mistral-large-3-2512"]
AVAILABLE_MODELS = [{"id":f"piax/{m}","object":"model","owned_by":"piax"} for m in REAL_MODELS]

async def stream_chat(auth_token: str, messages: list[dict], model: str = "gpt-5", files=None):
    # Every OpenAI request is one independent Piax conversation.
    conversation_id = str(uuid.uuid4())
    model_name = model.removeprefix("piax/")
    # Resolve the model's real Piax agent id from the public catalog.
    try:
        async with httpx.AsyncClient(timeout=15) as catalog_client:
            page = (await catalog_client.get("https://www.piax.org/en/home")).text
        for mid, name, path in re.findall(r'\\"id\\":\\"(\d+)\\".*?\\"name\\":\\"([^\\"]+)\\".*?\\"path\\":\\"([^\\"]+)\\"', page):
            if path == model_name or name.lower() == model_name.lower(): MODEL_AGENT[model_name] = mid; break
    except Exception:
        pass
    # Piax accepts one query string; preserve the complete OpenAI history so
    # follow-up turns retain context instead of sending only the last message.
    history = []
    for m in messages:
        role, content = m.get("role", "user"), m.get("content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text": parts.append(item.get("text", ""))
                    elif item.get("type") in ("image_url", "image"): parts.append(f"[image: {item.get('image_url', item.get('url', ''))}]")
            content = "\n".join(parts)
        history.append(f"{role}: {content}")
    query = "\n".join(history)
    files = files or []
    agent_id = MODEL_AGENT.get(model_name)
    if not agent_id:
        raise ValueError(f"Unknown Piax model: {model_name}; call /v1/models first")
    payload = {"device": {"deviceType": "pc", "osPlatform": "web"}, "agentId": agent_id,
               "query": query, "conversationId": conversation_id, "parentMessageId": "", "files": files,
               "inputs": {"tools.enable":"true", "tools.network":"true", "tools.fetch_webpage":"true",
                          "tools.reasoning":"false", "deep_research_mode":"false", "reasoning.exclude":"true",
                          "tools.preference":"false", "preferences.inject":"false", "private_mode":"false"}}
    headers = {"accept": "text/event-stream, application/json", "content-type": "application/json",
               "authorization": auth_token, "origin": "https://www.piax.org", "referer": "https://www.piax.org/"}
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("POST", BASE + CHAT_PATH, headers=headers, json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line: yield line if line.startswith("data:") else "data: " + line

async def upload_file_to_arting(auth_token: str, image_url_or_base64: str) -> dict:
    # Piax accepts image references directly in the chatStream `files` array.
    if not image_url_or_base64:
        return {}
    return {"url": image_url_or_base64, "type": "image"}

async def claim_daily(token: str) -> dict:
    headers = {"authorization": token, "content-type": "application/json", "origin": "https://www.piax.org"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(BASE + "/trade-api/trade/daily/claim", headers=headers,
                         json={"device": {"deviceType": "pc", "osPlatform": "web"}})
        return r.json()

async def get_balance(token: str) -> dict:
    headers = {"authorization": token, "content-type": "application/json", "origin": "https://www.piax.org"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(BASE + "/trade-api/trade/subscription/getUserSubscriptionStatus", headers=headers,
                         json={"productNo": "pia", "device": {"deviceType": "pc", "osPlatform": "web"}})
        return r.json()
