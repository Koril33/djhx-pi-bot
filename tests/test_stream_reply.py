import asyncio
import time
import unittest
from unittest.mock import patch

import bot


class _FakeMessage:
    async def reply_text(self, *args, **kwargs):
        raise AssertionError("reply_text should be routed through the patched _send")


class _FakeUpdate:
    message = _FakeMessage()
    callback_query = None


class _SentMessage:
    pass


class StreamReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_deltas_are_coalesced_before_first_send(self) -> None:
        calls: list[tuple[float, str, bool]] = []

        async def fake_send(send_fn, text, *, edit=False, msg=None, reply_markup=None):
            calls.append((time.monotonic(), text, edit))
            return True if edit else _SentMessage()

        async def deltas():
            yield "你"
            await asyncio.sleep(0.01)
            yield "好"
            await asyncio.sleep(0.01)
            yield "！"
            await asyncio.sleep(0.08)
            yield "后续"
            await asyncio.sleep(0.08)

        with patch.object(bot, "_send", side_effect=fake_send):
            await bot.stream_reply(
                _FakeUpdate(),
                deltas(),
                interval=0.05,
                initial_delay=0.05,
                initial_chars=10,
            )

        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0][1], "你好！")
        self.assertEqual(calls[-1][1], "你好！后续")
        self.assertFalse(calls[0][2])
        self.assertTrue(calls[-1][2])

    async def test_markdown_split_does_not_run_for_every_delta(self) -> None:
        split_calls = 0
        real_split = bot.split_markdown

        def counted_split(text, max_html_len=bot.MAX_HTML_LEN):
            nonlocal split_calls
            split_calls += 1
            return real_split(text, max_html_len)

        async def fake_send(send_fn, text, *, edit=False, msg=None, reply_markup=None):
            return True if edit else _SentMessage()

        async def deltas():
            for _ in range(100):
                yield "x"

        with (
            patch.object(bot, "_send", side_effect=fake_send),
            patch.object(bot, "split_markdown", side_effect=counted_split),
        ):
            await bot.stream_reply(
                _FakeUpdate(),
                deltas(),
                interval=0.05,
                initial_delay=0.05,
                initial_chars=1000,
            )

        self.assertEqual(split_calls, 1)


if __name__ == "__main__":
    unittest.main()
