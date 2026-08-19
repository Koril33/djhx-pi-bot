"""FastAPI 入口：Telegram webhook + 健康检查。

部署形态：nginx (TLS) → uvicorn (127.0.0.1:8128) → FastAPI
webhook 只做快速 ack，真正的消息处理放到后台任务（避免占用 Telegram 回执超时）。
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from telegram import Update

from bot import AgentManager, build_application
from config import get_settings
from logging_conf import get_logger, setup_logging

logger = get_logger("app")

settings = get_settings()
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from storage import Storage

    storage = Storage(settings.db_path)
    manager = AgentManager(settings, storage)
    tg_app = build_application(settings, storage, manager)
    await tg_app.initialize()
    await tg_app.start()
    app.state.storage = storage
    app.state.manager = manager
    app.state.tg_app = tg_app
    logger.info(
        "djhx-pi-bot 启动完成",
        extra={
            "port": settings.port,
            "env_count": len(settings.allowed_ids),
            "pi_cmd": settings.pi_cmd,
        },
    )
    try:
        yield
    finally:
        await manager.shutdown()
        await tg_app.stop()
        await tg_app.shutdown()


app = FastAPI(title="djhx-pi-bot", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "djhx-pi-bot"}


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request) -> Response:
    if secret != settings.webhook_secret:
        logger.warning("webhook 密钥不匹配")
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    update = Update.de_json(data, app.state.tg_app.bot)
    if update is None:
        return Response(status_code=status.HTTP_200_OK)

    # 后台处理，快速 ack（Telegram 等待响应约 60s）
    asyncio.create_task(app.state.tg_app.process_update(update))
    return Response(status_code=status.HTTP_200_OK)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=settings.host, port=settings.port, log_config=None)
