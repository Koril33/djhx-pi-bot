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
            effective_chat=self.chat, message=self.message,
        )
        self.storage = Mock()
        self.storage.get_active_session.return_value = {"id": "s1", "name": "已命名"}
        self.calls = []

        async def prompt(text, **kwargs):
            self.calls.append((text, kwargs))
            yield "红色"

        self.client = SimpleNamespace(
            busy=False,
            get_state=AsyncMock(return_value={"model": {"input": ["text", "image"]}}),
            prompt=prompt,
            steer=AsyncMock(),
        )
        self.agent = SimpleNamespace(
            client=self.client, ensure_client=AsyncMock(), prompt_pending=False,
            control_lock=asyncio.Lock(), storage=self.storage,
        )
        self.manager = Mock()
        self.manager.get.return_value = self.agent
        self.context = SimpleNamespace(bot_data={
            "settings": SimpleNamespace(
                stream_edit_interval=0.1, stream_initial_delay=0,
                stream_initial_chars=1,
            ),
            "storage": self.storage,
            "manager": self.manager,
        })

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

    async def test_unsupported_model_is_explained_without_download(self):
        self.client.get_state.return_value = {"model": {"input": ["text"]}}
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.on_text(self.update, self.context)
        self.assertIn("不支持图片", reply.await_args.args[1])
        self.media.get_file.assert_not_awaited()
        self.assertFalse(self.agent.prompt_pending)

    async def test_missing_model_capability_is_explained_without_download(self):
        self.client.get_state.return_value = {"model": {"id": "unknown"}}
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.on_text(self.update, self.context)
        self.assertIn("无法确认", reply.await_args.args[1])
        self.media.get_file.assert_not_awaited()

    async def test_busy_photo_steers_with_image(self):
        self.client.busy = True
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.on_text(self.update, self.context)
        self.client.steer.assert_awaited_once()
        args, kwargs = self.client.steer.await_args
        self.assertEqual(args, ("图片里是什么颜色？",))
        self.assertEqual(kwargs["images"][0]["mimeType"], "image/png")
        self.assertIn("已收到", reply.await_args.args[1])

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

    async def test_album_is_rejected_once_without_starting_agent(self):
        self.message.media_group_id = "album-1"
        with patch.object(bot, "reply_text", new_callable=AsyncMock) as reply:
            await bot.on_text(self.update, self.context)
            await bot.on_text(self.update, self.context)
        reply.assert_awaited_once()
        self.assertIn("相册", reply.await_args.args[1])
        self.manager.get.assert_not_called()

    def test_photo_and_image_document_reach_image_handler(self):
        settings = SimpleNamespace(bot_token="123:ABC", allowed_ids={1})
        app = bot.build_application(settings, self.storage, self.manager)
        handler = app.handlers[0][-2]
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
        await app.handlers[0][-2].callback(self.update, self.context)
        self.message.reply_text.assert_awaited_once()
        self.manager.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
