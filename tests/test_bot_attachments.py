import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import Bot, Update
from telegram.error import BadRequest

import bot
from config import Settings
from interactions import PendingActions
from storage import Storage
from tests.test_bot_images import PNG, media_with_data


class AttachmentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.folder.name) / "bot.db"))
        self.sid = self.storage.create_session(7, "图片讨论")
        self.storage.set_active(self.sid)
        self.storage.set_pi_session_file(self.sid, "/missing/empty.jsonl", known_empty=True)
        self.actions = PendingActions(self.storage.db_path)
        self.settings = Settings(_env_file=None, bot_token="test", webhook_secret="test",
                                 allowed_user_ids="7,8", memory_enabled=False,
                                 pi_session_dir=self.folder.name)
        self.tg = Mock(spec=Bot)
        self.tg.get_file = AsyncMock(return_value=SimpleNamespace(
            file_size=len(PNG), download_as_bytearray=AsyncMock(return_value=bytearray(PNG))))
        self.tg.send_chat_action = AsyncMock()
        self.tg.answer_callback_query = AsyncMock()
        self.calls = []

        async def prompt(text, **kwargs):
            self.calls.append((text, kwargs))
            yield "看到了红色。"

        async def set_model(value):
            self.client.get_state.return_value = {"model": {"input": ["text", "image"]}}
            return {"model": {"provider": "test", "id": "vision"}}

        self.client = SimpleNamespace(busy=False, get_state=AsyncMock(return_value={
            "model": {"input": ["text"]}}), get_available_models=AsyncMock(return_value=[
            {"provider": "test", "id": "vision", "input": ["text", "image"]}]),
            set_model=AsyncMock(side_effect=set_model), prompt=prompt)
        self.agent = SimpleNamespace(user_id=7, client=self.client, ensure_client=AsyncMock(),
            prompt_pending=False, control_lock=asyncio.Lock(), turn_tail=None, active_turn_id=None)
        self.manager = SimpleNamespace(get=lambda user: self.agent, actions=self.actions, memory=None)
        self.context = SimpleNamespace(bot=self.tg, args=[], bot_data={
            "settings": self.settings, "storage": self.storage, "manager": self.manager})
        self.reply_patch = patch.object(bot, "reply_text", new_callable=AsyncMock)
        self.reply = self.reply_patch.start()

        async def consume(update, stream, *_args, **_kwargs):
            async for part in stream:
                pass
        self.stream_patch = patch.object(bot, "stream_reply", side_effect=consume)
        self.stream_patch.start()

    async def asyncTearDown(self):
        self.stream_patch.stop()
        self.reply_patch.stop()
        self.actions.close()
        self.storage._conn.close()
        self.folder.cleanup()

    def update(self, *, uid=7, number=1, document=None, caption="图片里是什么颜色？"):
        message = {"message_id": number, "date": 1700000000, "from": {
            "id": uid, "is_bot": False, "first_name": "User"},
            "chat": {"id": uid, "type": "private"}, "caption": caption}
        message["document" if document else "photo"] = document or [{
            "file_id": "photo", "file_unique_id": "unique", "width": 2,
            "height": 2, "file_size": len(PNG)}]
        return Update.de_json({"update_id": number, "message": message}, self.tg)

    def callback(self, data, uid=7, number=50):
        return Update.de_json({"update_id": number, "callback_query": {
            "id": "query", "chat_instance": "instance", "data": data,
            "from": {"id": uid, "is_bot": False, "first_name": "User"},
            "message": {"message_id": 49, "date": 1700000000,
                        "chat": {"id": uid, "type": "private"}, "text": "选择模型"}}}, self.tg)

    async def pending_image(self):
        await bot.on_text(self.update(), self.context)
        self.assertEqual(self.calls, [])
        return self.reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    async def test_real_telegram_payload_switches_model_and_continues_once(self):
        callback = await self.pending_image()
        self.tg.answer_callback_query.side_effect = BadRequest("Query is too old")
        await bot.on_callback(self.callback(callback), self.context)
        await bot.on_callback(self.callback(callback, number=51), self.context)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("图片里是什么颜色", self.calls[0][0])
        self.assertEqual(len(self.calls[0][1]["images"]), 1)
        selected = self.storage.get_effective_model_selection(7, self.sid)
        self.assertEqual((selected["provider"], selected["model_id"]), ("test", "vision"))
        self.assertIsNone(self.storage.get_user_model(7))
        self.client.set_model.assert_awaited_once_with("test/vision")

    async def test_foreign_user_and_switched_session_do_not_consume_pending_image(self):
        callback = await self.pending_image()
        ident = callback.split(":")[1]
        await bot.on_callback(self.callback(callback, uid=8), self.context)
        other = self.storage.create_session(7, "其他会话")
        self.storage.set_active(other)
        await bot.on_callback(self.callback(callback), self.context)
        self.assertIsNotNone(self.actions.get(7, ident, "image"))
        self.client.set_model.assert_not_awaited()
        self.assertEqual(self.calls, [])

    async def test_album_reuses_saved_files_even_after_original_inbox_is_done(self):
        originals = [self.update(number=11), self.update(number=12, caption=None)]
        for update in originals:
            self.storage.enqueue_update(update.update_id, "{}", user_id=7, queueable=True)
            self.storage.claim_updates(1)
            self.storage.complete_update(update.update_id)
        markup, saved = await bot.image_continue_markup(originals[0], self.context,
                                                        self.client, originals)
        self.assertTrue(saved)
        await bot.on_callback(self.callback(markup.inline_keyboard[0][0].callback_data), self.context)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0][1]["images"]), 2)

    async def test_confirmed_text_checks_session_again_after_fifo_wait(self):
        first = asyncio.get_running_loop().create_future()
        self.agent.turn_tail = first
        task = asyncio.create_task(bot.process_confirmed_attachment(self.update(), self.context,
                                                                    expected_session_id=self.sid))
        await asyncio.sleep(0)
        self.storage.set_active(self.storage.create_session(7, "其他会话"))
        first.set_result(None)
        await task
        self.assertEqual(self.calls, [])
        self.agent.ensure_client.assert_not_awaited()

    async def test_pdf_includes_caption_and_exact_extraction_coverage(self):
        update = self.update(document={"file_id": "pdf", "file_unique_id": "pdf",
            "file_name": "notes.pdf", "mime_type": "application/pdf", "file_size": 10},
            caption="给我总结前三页")
        result = {"text": "测试正文", "pages": 40, "read_pages": 30, "truncated": True}
        self.tg.get_file.return_value.download_as_bytearray.return_value = bytearray(b"%PDF-test")
        with patch.object(bot, "extract_pdf", new_callable=AsyncMock, return_value=result) as extract:
            await bot.on_text(update, self.context)
        extract.assert_awaited_once()
        self.assertIn("给我总结前三页", self.calls[0][0])
        self.assertIn("30/40 页", self.calls[0][0])
        self.assertIn("仅分析以下可见内容", self.calls[0][0])
        self.assertIn("测试正文", self.calls[0][0])
        self.assertIsNone(self.calls[0][1]["images"])

    async def test_pdf_worker_rejection_never_starts_prompt(self):
        update = self.update(document={"file_id": "pdf", "file_unique_id": "pdf",
            "file_name": "scan.pdf", "mime_type": "application/pdf"})
        with patch.object(bot, "extract_pdf", new_callable=AsyncMock,
                          side_effect=ValueError("PDF 没有可提取文本，扫描件请先 OCR")):
            await bot.on_text(update, self.context)
        self.assertEqual(self.calls, [])
        self.assertIn("扫描件", self.reply.await_args.args[1])

    async def test_pdf_size_checked_before_and_after_download(self):
        media = media_with_data(b"%PDF-test")
        media.file_size = self.settings.pdf_max_bytes + 1
        with self.assertRaises(ValueError):
            await bot._download_pdf_document(media, self.settings)
        media.get_file.assert_not_awaited()
        media.file_size = None
        media.get_file.return_value.file_size = None
        self.settings.pdf_max_bytes = 5
        with patch.object(bot, "extract_pdf", new_callable=AsyncMock) as extract:
            with self.assertRaises(ValueError):
                await bot._download_pdf_document(media, self.settings)
        extract.assert_not_awaited()

    async def test_stop_during_pdf_extraction_cancels_worker_and_never_prompts(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def extract(data, settings):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        update = self.update(document={"file_id": "pdf", "file_unique_id": "pdf", "file_name": "notes.pdf"})
        with patch.object(bot, "extract_pdf", side_effect=extract):
            task = asyncio.create_task(bot.on_text(update, self.context))
            await entered.wait()
            await bot.cmd_stop(update, self.context)
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.calls, [])
