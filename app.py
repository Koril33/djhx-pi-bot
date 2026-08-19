"""FastAPI 入口：Telegram webhook + 健康检查。

部署形态：nginx (TLS) → uvicorn (127.0.0.1:8128) → FastAPI
webhook 只做快速 ack，真正的消息处理放到后台任务（避免占用 Telegram 回执超时）。
"""
import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

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
    # start() 内部会调 getMe；服务器→api.telegram.org 偶发网络错误会导致整个服务退出，加重试
    for attempt in range(5):
        try:
            await tg_app.start()
            break
        except Exception as e:
            logger.warning("tg_app.start 失败，重试", extra={"attempt": attempt + 1, "err": str(e)[:200]})
            await asyncio.sleep(3)
    else:
        raise RuntimeError("tg_app.start 多次失败")
    # post_init 只被 run_polling/run_webhook 自动调用，手动模式需显式执行（注册命令菜单等）
    try:
        await tg_app.post_init(tg_app)
    except Exception as e:
        logger.warning("post_init 执行失败", extra={"err": str(e)[:300]})
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


@app.post("/deploy-webhook/{secret}")
async def deploy_webhook(secret: str) -> Response:
    """Gitea Push webhook 入口：验证密钥后，后台 detached 执行 deploy.sh 并立即返回。

    deploy.sh 会 git pull + uv sync + systemctl restart（重启会杀掉本进程），
    所以必须用 start_new_session 分离出独立进程，且先返回 200。
    """
    if secret != settings.deploy_secret:
        logger.warning("deploy webhook 密钥不匹配")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    repo_root = Path(__file__).resolve().parent
    deploy_script = repo_root / "deploy" / "deploy.sh"
    if not deploy_script.exists():
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    os.makedirs(repo_root / "logs", exist_ok=True)
    log_file = open(repo_root / "logs" / "deploy.log", "ab")
    subprocess.Popen(
        ["bash", str(deploy_script)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(repo_root),
    )
    logger.info("收到 Gitea push，已触发部署")
    return Response(status_code=status.HTTP_200_OK)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=settings.host, port=settings.port, log_config=None)
