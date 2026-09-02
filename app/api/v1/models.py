"""
GET /v1/models
返回当前支持的模型列表（OpenAI 兼容格式）。
"""

import time
import re, httpx
from fastapi import APIRouter, Depends
from app.services.arting_client import AVAILABLE_MODELS
from app.core.auth import verify_api_key

router = APIRouter()


@router.get(
    "/models",
    summary="List Models (OpenAI Compatible)",
    dependencies=[Depends(verify_api_key)],
)
async def list_models():
    try:
        html=(await httpx.AsyncClient(timeout=20).get('https://www.piax.org/en/home')).text
        found=re.findall(r'\\"id\\":\\"(\d+)\\".*?\\"name\\":\\"([^\\"]+)\\".*?\\"path\\":\\"([^\\"]+)\\"',html)
        if found:
            from app.services.arting_client import MODEL_AGENT
            MODEL_AGENT.update({p:i for i,n,p in found})
            return {"object":"list","data":[{"id":f"piax/{p}","object":"model","owned_by":"piax","created":int(time.time()),"name":n,"agent_id":i} for i,n,p in found]}
    except Exception:
        pass
    return {
        "object": "list",
        "data": [
            {**m, "created": int(time.time())}
            for m in AVAILABLE_MODELS
        ],
    }


@router.get(
    "/models/{model_id}",
    summary="Retrieve Model",
    dependencies=[Depends(verify_api_key)],
)
async def get_model(model_id: str):
    for m in AVAILABLE_MODELS:
        if m["id"] == model_id:
            return {**m, "created": int(time.time())}
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found.")
