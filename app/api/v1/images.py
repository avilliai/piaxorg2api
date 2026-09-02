import time, uuid
from typing import Optional
import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from app.core.auth import verify_api_key
from app.core.dependencies import get_token_manager
from app.services.arting_client import BASE

router = APIRouter()
class ImageRequest(BaseModel):
    prompt: str
    model: str = "gpt-image-2"
    n: int = 1
    size: str = "1024x1024"
    response_format: str = "url"

@router.post('/images/generations', dependencies=[Depends(verify_api_key)])
async def generations(req: ImageRequest, token_manager=Depends(get_token_manager)):
    token = await token_manager.get_next()
    headers = {'authorization': token, 'content-type':'application/json', 'origin':'https://www.piax.org'}
    payload = {'model': req.model, 'prompt': req.prompt, 'n': req.n, 'size': req.size}
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(BASE + '/ai-api/ai/images/gptImageTaskSubmit', headers=headers, json=payload)
        r.raise_for_status(); body = r.json()
    items = body.get('data') or body.get('images') or []
    if isinstance(items, dict): items=[items]
    data=[]
    for x in items:
        if isinstance(x,str): data.append({'url':x} if req.response_format=='url' else {'b64_json':x})
        else: data.append({k:x[k] for k in ('url','b64_json') if k in x})
    return {'created': int(time.time()), 'data': data}
