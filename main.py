"""
主入口：直接运行 `python main.py` 或 `uvicorn main:app`。傻逼github
"""

import logging
import os
from dotenv import load_dotenv
load_dotenv()
# 日志格式配置
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

from app.app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8001")),
        reload=os.getenv("RELOAD", "false").lower() == "true",
    )
