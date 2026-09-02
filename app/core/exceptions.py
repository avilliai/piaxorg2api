"""
统一异常处理：将各类异常映射为 OpenAI 风格的错误响应。
"""

import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _error_body(status_code: int, message: str, err_type: str = "api_error") -> dict:
    return {
        "error": {
            "message": message,
            "type": err_type,
            "code": status_code,
        }
    }


def register_exception_handlers(app: FastAPI):

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.status_code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content=_error_body(500, "Internal server error", "internal_error"),
        )
