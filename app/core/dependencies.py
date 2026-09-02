"""
FastAPI 依赖注入：将单例对象（TokenManager 等）注入到路由处理函数。
"""

from fastapi import Request
from app.services.token_manager import TokenManager


def get_token_manager(request: Request) -> TokenManager:
    return request.app.state.token_manager
