"""
API Key 鉴权：支持 Bearer Token 和直接 Header 两种方式。
若 API_KEY 环境变量未设置，则跳过鉴权（开发模式）。
"""

import os
import logging
from fastapi import Header, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
from dotenv import load_dotenv
load_dotenv()
API_KEY = os.getenv("API_KEY", "")  # 留空 = 不鉴权
logger.info(f"API_KEY: {API_KEY}")


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
):
    if not API_KEY:
        # 未配置 API_KEY，开发模式直接放行
        return

    provided = credentials.credentials if credentials else None
    if provided != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key. Set Authorization: Bearer <your_api_key>",
            headers={"WWW-Authenticate": "Bearer"},
        )
