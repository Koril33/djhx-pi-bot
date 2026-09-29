import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import Update
from telegram.error import NetworkError

import bot


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="
)


def media_with_data(data=PNG, *, file_size=None):
    file = SimpleNamespace(
        file_size=len(data) if file_size is None else file_size,
        download_as_bytearray=AsyncMock(return_value=bytearray(data)),
    )
    return SimpleNamespace(
        file_size=len(data) if file_size is None else file_size,
        get_file=AsyncMock(return_value=file),
    )


class ImageDownloadTests(unittest.IsolatedAsyncioTestCase):
    def test_supported_signatures(self):
        self.assertEqual(bot._image_mime_type(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(bot._image_mime_type(b"GIF89a" + b"\x00" * 7), "image/gif")
        self.assertEqual(bot._image_mime_type(b"RIFF" + b"\x00" * 4 + b"WEBPVP8 "), "image/webp")

    def test_common_image_headers_have_dimensions(self):
        jpeg = (
            b"\xff\xd8\xff\xe0\x00\x04ab"
            b"\xff\xc0\x00\x0b\x08\x00\x10\x00\x20\x01\x01\x11\x00"
            b"\xff\xd9"
        )
        webp = b"RIFF" + (22).to_bytes(4, "little") + b"WEBPVP8X" + (10).to_bytes(4, "little")
        webp += b"\x00" * 4 + b"\x1f\x00\x00" + b"\x0f\x00\x00"
        self.assertEqual(bot._image_dimensions(PNG, "image/png"), (1, 1))
        self.assertEqual(bot._image_dimensions(jpeg, "image/jpeg"), (32, 16))
        self.assertEqual(bot._image_dimensions(b"GIF89a\x20\x00\x10\x00" + b"\x00" * 3, "image/gif"), (32, 16))
        self.assertEqual(bot._image_dimensions(webp, "image/webp"), (32, 16))

    async def test_text_document_size_and_encoding_are_checked(self):
        document = media_with_data(b"hello", file_size=bot.MAX_TEXT_FILE_BYTES + 1)
        document.file_name = "large.log"
        document.mime_type = "text/plain"
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            await bot._download_text_document(document)
        document.get_file.assert_not_awaited()

        document = media_with_data(b"\xff\xfe")
        document.file_name = "invalid.txt"
        document.mime_type = "text/plain"
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            await bot._download_text_document(document)

        document = media_with_data(b"hello\x00world")
        document.file_name = "binary.txt"
        document.mime_type = "text/plain"
        with self.assertRaisesRegex(ValueError, "二进制"):
            await bot._download_text_document(document)

        document = media_with_data(b"%PDF-1.7 test")
        document.file_name = "renamed.txt"
        document.mime_type = "text/plain"
        with self.assertRaisesRegex(ValueError, "二进制"):
            await bot._download_text_document(document)

    async def test_photo_uses_largest_size_and_real_mime(self):
        small = media_with_data(b"bad")
        large = media_with_data(PNG)
        message = SimpleNamespace(photo=[small, large], document=None)
        result = await bot._download_image(message)
        self.assertEqual(result, {
            "type": "image",
            "data": base64.b64encode(PNG).decode("ascii"),
            "mimeType": "image/png",
        })
        small.get_file.assert_not_awaited()

    async def test_document_checks_bytes_instead_of_claimed_mime(self):
        document = media_with_data(PNG)
        document.mime_type = "image/jpeg"
        message = SimpleNamespace(photo=[], document=document)
        result = await bot._download_image(message)
        self.assertEqual(result["mimeType"], "image/png")

    async def test_declared_oversize_is_rejected_before_download(self):
        media = media_with_data(PNG, file_size=bot.MAX_IMAGE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "10 MiB"):
            await bot._download_image(SimpleNamespace(photo=[media], document=None))
        media.get_file.assert_not_awaited()

    async def test_get_file_oversize_is_rejected_before_download(self):
        media = media_with_data(PNG)
        media.file_size = None
        media.get_file.return_value.file_size = bot.MAX_IMAGE_BYTES + 1
        with self.assertRaisesRegex(ValueError, "10 MiB"):
            await bot._download_image(SimpleNamespace(photo=[media], document=None))
        media.get_file.return_value.download_as_bytearray.assert_not_awaited()

    async def test_actual_oversize_is_rejected(self):
        media = media_with_data(PNG + b"x" * bot.MAX_IMAGE_BYTES)
        media.file_size = None
        media.get_file.return_value.file_size = None
        with self.assertRaisesRegex(ValueError, "10 MiB"):
            await bot._download_image(SimpleNamespace(photo=[], document=media))

    async def test_fake_image_is_rejected(self):
        media = media_with_data(b"%PDF-1.7")
        with self.assertRaisesRegex(ValueError, "格式无效"):
            await bot._download_image(SimpleNamespace(photo=[], document=media))

    async def test_oversize_dimensions_are_rejected_before_pi(self):
        oversized = bytearray(PNG)
        oversized[16:20] = (8193).to_bytes(4, "big")
        media = media_with_data(oversized)
        with self.assertRaisesRegex(ValueError, "8192"):
            await bot._download_image(SimpleNamespace(photo=[], document=media))

    async def test_download_failure_does_not_expose_file_url(self):
        media = media_with_data(PNG)
        media.get_file.side_effect = RuntimeError("https://api.telegram.org/file/botSECRET/image.png")
        with self.assertRaises(ValueError) as raised:
            await bot._download_image(SimpleNamespace(photo=[media], document=None), user_id=1)
        self.assertEqual(str(raised.exception), "图片下载失败，请稍后重发")

    async def test_transient_telegram_download_error_is_retried(self):
        media = media_with_data(PNG)
        media.get_file.return_value.download_as_bytearray.side_effect = [
            NetworkError("connection lost"), bytearray(PNG),
        ]
        with patch.object(bot.asyncio, "sleep", new_callable=AsyncMock):
            result = await bot._download_image(SimpleNamespace(photo=[], document=media))
        self.assertEqual(result["mimeType"], "image/png")
        self.assertEqual(media.get_file.await_count, 2)

    async def test_repeated_telegram_download_error_has_safe_feedback(self):
        media = media_with_data(PNG)
        media.get_file.side_effect = NetworkError("https://api.telegram.org/file/botSECRET/image.png")
        with patch.object(bot.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(ValueError) as raised:
                await bot._download_image(SimpleNamespace(photo=[], document=media))
        self.assertEqual(str(raised.exception), "图片下载失败，请稍后重发")
        self.assertEqual(media.get_file.await_count, 3)


class ImageMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.media = media_with_data()
        self.message = SimpleNamespace(
            photo=[self.media], document=None, text=None, caption="图片里是什么颜色？",
            media_group_id=None, reply_text=AsyncMock(),
        )
        self.chat = SimpleNamespace(id=1, send_chat_action=AsyncMock())
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1, username="tester"),
            effective_chat=self.chat, message=self.message, callback_query=None,
        )
        self.storage = Mock()
        self.storage.get_active_session.return_value = {"id": "s1", "name": "已命名"}
        self.storage.pending_interruption_notice_count.return_value = 0
        self.storage.begin_update.return_value = True
        self.storage.count_user_queue.return_value = 1
        self.storage.list_pending_mail_drafts.return_value = []
        self.storage.get_pending_failure_notices.return_value = []
        self.calls = []

        async def prompt(text, **kwargs):
            self.calls.append((text, kwargs))
            yield "红色"

        self.client = SimpleNamespace(
            busy=False,
            get_state=AsyncMock(return_value={"model": {"input": ["text", "image"]}}),
            get_available_models=AsyncMock(return_value=[]),
            prompt=prompt,
            steer=AsyncMock(),
        )
        self.agent = SimpleNamespace(
            client=self.client, ensure_client=AsyncMock(), prompt_pending=False,
            control_lock=asyncio.Lock(), turn_tail=None,
            active_turn_id=None, storage=self.storage,
        )
        self.manager = Mock()
        self.manager.get.return_value = self.agent
        self.manager.memory = None
        self.context = SimpleNamespace(bot_data={
            "settings": SimpleNamespace(
                stream_edit_interval=0.1, stream_initial_delay=0,
                stream_initial_chars=1,
            ),
            "storage": self.storage,
            "manager": self.manager,
        })

    async def test_text_document_reaches_prompt_without_image_requirement(self):
        data = "print('你好')\n".encode()
        self.media = media_with_data(data)
        self.media.file_name = "example.py"
        self.media.mime_type = "text/x-python"
        self.message.photo = []
        self.message.document = self.media
        self.message.caption = "解释这段代码"

        async def consume(_update, stream, *_args, **_kwargs):
            _ = [part async for part in stream]

        with patch.object(bot, "stream_reply", side_effect=consume):
            await bot.on_text(self.update, self.context)
        prompt, kwargs = self.calls[0]
        self.assertIn("解释这段代码", prompt)
        self.assertIn("example.py", prompt)
        self.assertIn("print('你好')", prompt)
        self.assertIsNone(kwargs["images"])
        self.client.get_state.assert_not_awaited()

    async def test_unsupported_document_is_explained_before_download(self):
        self.media.file_name = "report.pdf"
        self.media.mime_type = "application/pdf"
        self.message.photo = []
        self.message.document = self.media
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.on_text(self.update, self.context)
        self.assertIn("PDF", reply.await_args.args[1])
        self.media.get_file.assert_not_awaited()
        self.agent.ensure_client.assert_not_awaited()
        self.assertEqual(self.calls, [])

    async def test_text_document_caption_can_steer(self):
        self.media = media_with_data(b"safe test content")
        self.media.file_name = "notes.txt"
        self.media.mime_type = "text/plain"
        self.message.photo = []
        self.message.document = self.media
        self.message.caption = "/steer 请参考附件"
        self.client.busy = True
        with patch.object(bot, "reply_text", new_callable=AsyncMock):
            await bot.on_text(self.update, self.context)
        instruction = self.client.steer.await_args.args[0]
        self.assertIn("safe test content", instruction)
        self.assertIn("请参考附件", instruction)

    async def test_prompt_completion_delivers_pending_mail_preview(self):
        draft = {
            "id": "abcdef012345", "recipient": "owner@example.com",
            "subject": "测试", "body": "确认前不得发送",
        }
        self.storage.list_pending_mail_drafts.return_value = [draft]

        async def consume(_update, stream, *_args, **_kwargs):
            _ = [part async for part in stream]

        with patch.object(bot, "stream_reply", side_effect=consume):
            await bot.on_text(self.update, self.context)
        preview = "".join(call.args[0] for call in self.message.reply_text.await_args_list)
        self.assertIn("收件人：owner@example.com", preview)
        self.assertIn("确认前不得发送", preview)
        self.storage.mark_mail_previewed.assert_called_once_with(1, "abcdef012345")

    async def test_photo_caption_is_sent_with_image_to_prompt(self):
        replies = []

        async def consume(_update, stream, *_args, **_kwargs):
            replies.extend([part async for part in stream])

        with patch.object(bot, "stream_reply", side_effect=consume):
            await bot.on_text(self.update, self.context)
        self.assertEqual(replies, ["红色"])
        self.assertEqual(self.calls[0][0], "图片里是什么颜色？")
        self.assertEqual(self.calls[0][1]["images"][0]["mimeType"], "image/png")
        self.assertFalse(self.agent.prompt_pending)

    async def test_photo_without_caption_gets_default_question(self):
        self.message.caption = None

        async def consume(_update, stream, *_args, **_kwargs):
            _ = [part async for part in stream]

        with patch.object(bot, "stream_reply", side_effect=consume):
            await bot.on_text(self.update, self.context)
        self.assertEqual(self.calls[0][0], "请分析这张图片。")

    async def test_unsupported_model_offers_image_model_without_download(self):
        self.client.get_state.return_value = {"model": {"input": ["text"]}}
        self.client.get_available_models = AsyncMock(return_value=[
            {"provider": "deepseek", "id": "flash", "input": ["text", "image"]},
            {"provider": "deepseek", "id": "pro", "input": ["text"]},
        ])
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.on_text(self.update, self.context)
        self.assertIn("不支持图片", reply.await_args.args[1])
        self.assertIn("重新发送", reply.await_args.args[1])
        buttons = reply.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data, "model_use:deepseek/flash")
        self.media.get_file.assert_not_awaited()
        self.assertFalse(self.agent.prompt_pending)

    async def test_missing_model_capability_is_explained_without_download(self):
        self.client.get_state.return_value = {"model": {"id": "unknown"}}
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.on_text(self.update, self.context)
        self.assertIn("无法确认", reply.await_args.args[1])
        self.media.get_file.assert_not_awaited()

    async def test_explicit_steer_photo_uses_image(self):
        self.client.busy = True
        self.message.caption = "/steer 图片里是什么颜色？"
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.on_text(self.update, self.context)
        self.client.steer.assert_awaited_once()
        args, kwargs = self.client.steer.await_args
        self.assertEqual(args, ("图片里是什么颜色？",))
        self.assertEqual(kwargs["images"][0]["mimeType"], "image/png")
        self.assertIn("已插入当前任务", reply.await_args.args[1])

    async def test_busy_turn_finishing_during_download_uses_new_prompt(self):
        self.client.busy = True
        original_file = self.media.get_file.return_value

        async def finish_turn_during_download():
            self.client.busy = False
            return original_file

        self.media.get_file.side_effect = finish_turn_during_download

        async def consume(_update, stream, *_args, **_kwargs):
            _ = [part async for part in stream]

        with patch.object(bot, "stream_reply", side_effect=consume):
            await bot.on_text(self.update, self.context)
        self.client.steer.assert_not_awaited()
        self.assertEqual(self.calls[0][0], "图片里是什么颜色？")
        self.assertEqual(self.calls[0][1]["images"][0]["mimeType"], "image/png")

    async def test_image_download_holds_model_session_control_lock(self):
        download_started = asyncio.Event()
        finish_download = asyncio.Event()
        original_file = self.media.get_file.return_value

        async def delayed_file():
            download_started.set()
            await finish_download.wait()
            return original_file

        self.media.get_file.side_effect = delayed_file

        async def consume(_update, stream, *_args, **_kwargs):
            _ = [part async for part in stream]

        with patch.object(bot, "stream_reply", side_effect=consume):
            task = asyncio.create_task(bot.on_text(self.update, self.context))
            await asyncio.wait_for(download_started.wait(), 1)
            self.assertTrue(self.agent.control_lock.locked())
            finish_download.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(len(self.calls), 1)

    async def test_album_is_combined_in_message_order(self):
        self.message.media_group_id = "album-1"
        self.message.message_id = 2
        self.update.update_id = 102
        self.storage.begin_album_updates.return_value = True
        other_message = SimpleNamespace(
            photo=[media_with_data(PNG)], document=None, text=None,
            caption="按顺序看这组图片", media_group_id="album-1",
            message_id=1, reply_text=AsyncMock(),
        )
        other_update = SimpleNamespace(
            update_id=101, effective_user=self.update.effective_user,
            effective_chat=self.chat, message=other_message, callback_query=None,
        )

        async def consume(_update, stream, *_args, **_kwargs):
            _ = [part async for part in stream]

        with patch.object(bot, "ALBUM_QUIET_SECONDS", 0.02), patch.object(
            bot, "stream_reply", side_effect=consume,
        ), patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            first = asyncio.create_task(bot.on_text(self.update, self.context))
            await asyncio.sleep(0)
            second = asyncio.create_task(bot.on_text(other_update, self.context))
            await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0][1]["images"]), 2)
        self.assertEqual(self.calls[0][0], "按顺序看这组图片")
        self.storage.begin_album_updates.assert_called_once_with([101, 102])
        self.assertTrue(any("相册 2 张" in call.args[1] for call in reply.await_args_list))

    async def test_album_over_limit_is_rejected_before_download(self):
        self.message.media_group_id = "album-2"
        updates = []
        for number in range(5):
            item = SimpleNamespace(
                photo=[self.media], document=None, text=None, caption=None,
                media_group_id="album-2", message_id=number,
            )
            updates.append(SimpleNamespace(message=item, update_id=number))
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot._process_text(self.update, self.context, album_updates=updates)
        self.assertIn("相册共 5 张", reply.await_args.args[1])
        self.media.get_file.assert_not_awaited()
        self.manager.get.assert_not_called()

    def test_photo_and_image_document_reach_image_handler(self):
        settings = SimpleNamespace(bot_token="123:ABC", allowed_ids={1})
        app = bot.build_application(settings, self.storage, self.manager)
        handler = app.handlers[0][-3]
        base = {
            "update_id": 1,
            "message": {
                "message_id": 1, "date": 1,
                "chat": {"id": 1, "type": "private", "first_name": "Tester"},
                "from": {"id": 1, "is_bot": False, "first_name": "Tester"},
            },
        }
        for media in (
            {"photo": [{"file_id": "a", "file_unique_id": "b", "width": 1, "height": 1}]},
            {"document": {"file_id": "a", "file_unique_id": "b", "mime_type": "image/png"}},
        ):
            payload = {**base, "message": {**base["message"], **media}}
            self.assertTrue(handler.check_update(Update.de_json(payload, app.bot)))

    async def test_image_handler_uses_authorization_guard(self):
        settings = SimpleNamespace(bot_token="123:ABC", allowed_ids={2})
        app = bot.build_application(settings, self.storage, self.manager)
        self.message.chat = SimpleNamespace(type="private")
        self.update.callback_query = None
        await app.handlers[0][-3].callback(self.update, self.context)
        self.message.reply_text.assert_awaited_once()
        self.manager.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
