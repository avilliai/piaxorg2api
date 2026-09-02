import asyncio, logging, os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from app.services.token_manager import TokenManager
from app.api.v1 import chat, models, admin, anthropic, images
from app.core.exceptions import register_exception_handlers
load_dotenv(); logger=logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    tm=TokenManager(os.getenv('TOKEN_FILE','data/tokens.json')); await tm.load(); app.state.token_manager=tm
    try: await tm.refresh_all(); logger.info('Piax 启动探活完成（签到与余额已更新）')
    except Exception: logger.exception('Piax 启动探活异常，服务继续启动')
    task=asyncio.create_task(tm.keep_alive_loop(int(os.getenv('KEEP_ALIVE_MINUTES','30'))))
    yield
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    await tm.close()

def create_app():
    app=FastAPI(title='Piax2API',version='2.0.0',lifespan=lifespan)
    app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
    app.include_router(chat.router,prefix='/v1'); app.include_router(models.router,prefix='/v1')
    app.include_router(admin.router,prefix='/admin'); app.include_router(anthropic.router,prefix='/v1'); app.include_router(images.router,prefix='/v1')
    @app.get('/health')
    async def health(): return {'status':'ok'}
    register_exception_handlers(app); return app
