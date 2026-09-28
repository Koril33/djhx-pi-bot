"""FastAPI 入口：Telegram webhook、健康检查和 Gitea 部署入口。

部署形态：nginx (TLS) → uvicorn (127.0.0.1:8128) → FastAPI
Telegram 更新先写入 SQLite，再有界并发处理。处理中的更新若随服务中断，
其执行结果不确定，记录为失败供人工核对，避免重复执行 pi 工具调用。
"""
import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from telegram import Update

from admin import apply_runtime_settings, router as admin_router
from bot import AgentManager, build_application, failure_notice_text, register_command_menu
from config import get_settings
from logging_conf import get_logger, setup_logging
from storage import Storage, UpdateQueueFull

logger = get_logger("app")

settings = get_settings()
setup_logging()


def register_update_error_handler(tg_app, update_errors: dict[int, Exception]) -> None:
    """捕获 PTB 内部吞掉的 handler 异常，按 update_id 交回 inbox worker。"""

    async def on_error(update: object | None, context) -> None:
        exc = context.error
        update_id = update.update_id if isinstance(update, Update) else None
        if update_id is not None and exc is not None:
            update_errors.setdefault(update_id, exc)
        logger.error(
            "Telegram handler 异常",
            exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None,
            extra={"update_id": update_id, "err_type": type(exc).__name__ if exc else None},
        )

    # 必须 block=True：process_update 返回前，错误映射已写入。
    tg_app.add_error_handler(on_error, block=True)


async def process_queued_updates(
    storage: Storage,
    tg_app,
    wake: asyncio.Event,
    stopping: asyncio.Event,
    update_errors: dict[int, Exception],
) -> None:
    """有界并发分发；普通消息按 update_id 排队，为控制命令保留容量。"""

    async def process_one(row) -> None:
        update_id = row["update_id"]
        try:
            update = Update.de_json(json.loads(row["payload"]), tg_app.bot)
            if update is None:
                raise ValueError("Telegram 更新无法解析")
            await tg_app.process_update(update)
            # PTB 22 的 process_update 会调用 process_error 后正常返回。
            # 当前所有 bot handler 都以 block=True 执行，因此此处能看到其异常。
            handler_error = update_errors.pop(update_id, None)
            if handler_error is not None:
                raise handler_error
        except asyncio.CancelledError:
            # 处理中的更新状态不确定，finally 标为失败；避免重复执行工具调用。
            raise
        except Exception as exc:
            update_errors.pop(update_id, None)
            storage.fail_update(update_id, f"{type(exc).__name__}: {exc}")
            logger.exception(
                "Telegram 更新处理失败",
                extra={"update_id": update_id, "attempt": row["attempts"] + 1},
            )
            notice = storage.get_failure_notice(update_id)
            if notice is not None and notice["user_id"] in settings.allowed_ids:
                try:
                    await tg_app.bot.send_message(
                        chat_id=notice["user_id"], text=failure_notice_text(notice),
                    )
                    storage.mark_failure_notice_sent(update_id)
                except Exception as notify_exc:
                    logger.warning(
                        "失败更新通知暂未送达",
                        extra={"update_id": update_id, "err_type": type(notify_exc).__name__},
                    )
        else:
            storage.complete_update(update_id)

    in_flight: dict[asyncio.Task, int] = {}
    limits = {0: settings.webhook_worker_concurrency, 1: settings.webhook_steer_concurrency, 2: 1}
    last_prune = time.monotonic()
    try:
        while True:
            if time.monotonic() - last_prune >= 3600:
                storage.prune_updates()
                last_prune = time.monotonic()
            claimed = False
            if not stopping.is_set():
                # 同一批中先启动普通消息，再启动随后到达的 stop/steer，
                # 避免刚提交的 prompt 尚未标记 busy 时 /stop 抢先执行。
                for priority in (0, 1, 2):
                    available = limits[priority] - sum(p == priority for p in in_flight.values())
                    for row in storage.claim_updates(available, priority=priority):
                        task = asyncio.create_task(process_one(row), name=f"telegram-update-{row['update_id']}")
                        in_flight[task] = priority
                        claimed = True

            if stopping.is_set() and not in_flight and not claimed:
                return
            if in_flight:
                done, _ = await asyncio.wait(in_flight, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    del in_flight[task]
                    try:
                        task.result()
                    except Exception:
                        logger.exception("Telegram 更新任务异常退出")
            else:
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=0.5)
                except TimeoutError:
                    pass
    finally:
        for task in in_flight:
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
        interrupted = storage.fail_interrupted_updates()
        if interrupted:
            logger.error("Telegram 更新执行状态不确定，已停止自动重放", extra={"count": interrupted})


def _update_priority(update: Update, manager: AgentManager | None) -> int:
    """/stop 独占保留槽；/steer 和 /queue 使用控制槽。"""
    message = update.message
    text = (message.text or message.caption or "") if message is not None else ""
    callback = update.callback_query
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text.strip() else ""
    if command == "/stop" or (callback is not None and callback.data == "cmd:stop"):
        return 2
    if command in {"/steer", "/queue"}:
        return 1
    return 0



def _spawn_deploy(*, catch_up: bool = False) -> subprocess.Popen:
    repo_root = Path(__file__).resolve().parent
    deploy_script = repo_root / "deploy" / "deploy.sh"
    if not deploy_script.exists():
        raise FileNotFoundError(deploy_script)
    os.makedirs(repo_root / "logs", exist_ok=True)
    with open(repo_root / "logs" / "deploy.log", "ab") as log_file:
        return subprocess.Popen(
            ["bash", str(deploy_script), *(["--catch-up"] if catch_up else [])],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(repo_root),
        )


def _report_update_worker_exit(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception("Telegram 更新 worker 异常退出")


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage = Storage(settings.db_path)
    apply_runtime_settings(settings, storage)
    app.state.settings = settings
    storage.prune_updates()
    interrupted_turns = storage.fail_interrupted_turns()
    if interrupted_turns:
        logger.warning("上次服务中断留下未完成的会话回合，已标记为不完整",
                       extra={"count": interrupted_turns})
    interrupted = storage.fail_interrupted_updates()
    if interrupted:
        logger.error("上次服务中断留下未确认更新，需人工核对", extra={"count": interrupted})
    manager = AgentManager(settings, storage)
    tg_app = build_application(settings, storage, manager)
    update_errors: dict[int, Exception] = {}
    register_update_error_handler(tg_app, update_errors)
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
    # 手动启动模式不会触发 post_init，需显式注册命令菜单
    await register_command_menu(tg_app.bot)
    app.state.storage = storage
    app.state.manager = manager
    app.state.tg_app = tg_app
    app.state.update_wake = asyncio.Event()
    app.state.update_stopping = asyncio.Event()
    app.state.update_worker = asyncio.create_task(
        process_queued_updates(
            storage, tg_app, app.state.update_wake, app.state.update_stopping, update_errors
        ),
        name="telegram-update-worker",
    )
    app.state.update_worker.add_done_callback(_report_update_worker_exit)
    try:
        _spawn_deploy(catch_up=True)
    except OSError:
        logger.exception("启动部署补偿检查失败")
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
        app.state.update_stopping.set()
        app.state.update_wake.set()
        try:
            await asyncio.wait_for(app.state.update_worker, timeout=settings.shutdown_drain_seconds)
        except TimeoutError:
            logger.warning("Telegram 更新排空超时，剩余更新将在下次启动后处理")
            app.state.update_worker.cancel()
            await asyncio.gather(app.state.update_worker, return_exceptions=True)
        except Exception:
            logger.exception("Telegram 更新 worker 异常退出")
        await manager.shutdown()
        await tg_app.stop()
        await tg_app.shutdown()


app = FastAPI(title="djhx-pi-bot", lifespan=lifespan)
app.include_router(admin_router)


@app.get("/healthz")
async def healthz(request: Request):
    worker = getattr(request.app.state, "update_worker", None)
    ok = worker is not None and not worker.done()
    stats = request.app.state.storage.update_stats() if hasattr(request.app.state, "storage") else None
    return Response(
        content=json.dumps({
            "ok": ok,
            "degraded": not ok or bool(stats and stats["failed"]),
            "service": "djhx-pi-bot",
            "updates": stats,
        }),
        media_type="application/json",
        status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request) -> Response:
    if not settings.webhook_secret or not hmac.compare_digest(secret, settings.webhook_secret):
        logger.warning("webhook 密钥不匹配")
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    try:
        body = await request.body()
        if len(body) > settings.webhook_max_bytes:
            return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        data = json.loads(body)
        if not isinstance(data, dict):
            return Response(status_code=status.HTTP_400_BAD_REQUEST)
        update = Update.de_json(data, request.app.state.tg_app.bot)
        if update is None or not isinstance(update.update_id, int):
            return Response(status_code=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:
        logger.warning("Telegram 更新格式无效", extra={"err_type": type(exc).__name__})
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    try:
        message = update.message
        message_text = (message.text or message.caption or "") if message is not None else ""
        is_plain_message = bool(
            message is not None
            and (message_text or message.photo or message.document)
            and not message_text.lstrip().startswith("/")
        )
        user_id = update.effective_user.id if update.effective_user is not None else None
        active_session = request.app.state.storage.get_active_session(user_id) if user_id is not None else None
        inserted = request.app.state.storage.enqueue_update(
            update.update_id,
            body.decode("utf-8"),
            capacity=settings.webhook_queue_capacity,
            priority=_update_priority(update, getattr(request.app.state, "manager", None)),
            user_id=user_id,
            session_id=active_session["id"] if active_session is not None else None,
            queueable=is_plain_message,
        )
    except UpdateQueueFull:
        logger.error("Telegram 更新队列已满", extra={"update_id": update.update_id})
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception:
        logger.exception("Telegram 更新持久化失败", extra={"update_id": update.update_id})
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    if inserted:
        request.app.state.update_wake.set()
    return Response(status_code=status.HTTP_200_OK)


@app.post("/deploy-webhook/{secret}")
async def deploy_webhook(secret: str, request: Request) -> Response:
    """Gitea main push 入口；路径密钥与可选 HMAC 验证后触发部署。

    部署子进程会随 systemd 服务重启被结束；服务下次启动会补查远端提交。
    """
    if not settings.deploy_secret or not hmac.compare_digest(secret, settings.deploy_secret):
        logger.warning("deploy webhook 密钥不匹配")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    # Gitea 通常带签名；旧 hook 若尚未配置 Secret，仍允许路径密钥认证。
    # 有签名时必须验证，否则不能信任事件或请求体。
    body = await request.body()
    if len(body) > settings.webhook_max_bytes:
        return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    signature = request.headers.get("X-Gitea-Signature", "")
    if signature:
        expected = hmac.new(settings.deploy_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning("deploy webhook 签名不匹配")
            return Response(status_code=status.HTTP_403_FORBIDDEN)
    elif settings.deploy_require_signature:
        logger.warning("deploy webhook 缺少签名")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    event = request.headers.get("X-Gitea-Event", "")
    if event != "push":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    if not isinstance(payload, dict):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    if payload.get("ref") != "refs/heads/main":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    repo = payload.get("repository")
    if settings.deploy_repo_full_name and (
        not isinstance(repo, dict) or repo.get("full_name") != settings.deploy_repo_full_name
    ):
        logger.warning("deploy webhook 仓库不匹配")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        child = _spawn_deploy()
    except OSError:
        logger.exception("启动部署脚本失败")
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    logger.info(
        "收到 Gitea main push，已触发部署",
        extra={"delivery_id": request.headers.get("X-Gitea-Delivery", ""), "pid": child.pid},
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=settings.host, port=settings.port, log_config=None)
