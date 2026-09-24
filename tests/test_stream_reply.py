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

    async def test_empty_timeout_has_readable_error(self) -> None:
        calls: list[tuple[str, bool]] = []

        async def fake_send(send_fn, text, *, edit=False, msg=None, reply_markup=None):
            calls.append((text, edit))
            return True if edit else _SentMessage()

        async def deltas():
            yield "部分输出"
            raise TimeoutError()

        with patch.object(bot, "_send", side_effect=fake_send):
            await bot.stream_reply(_FakeUpdate(), deltas(), interval=0)

        self.assertIn("处理超时", calls[-1][0])
        self.assertNotIn("[输出中断: ]", calls[-1][0])
        self.assertFalse(calls[-1][1])

    async def test_tool_activity_refreshes_without_text_delta(self) -> None:
        calls: list[str] = []
        activity = {"current": None}

        async def fake_send(send_fn, text, *, edit=False, msg=None, reply_markup=None):
            calls.append(text)
            return True if edit else _SentMessage()

        async def deltas():
            activity["current"] = "🔍 读取: file.py"
            activity["notify"]()
            await asyncio.sleep(0.03)
            activity["current"] = None
            activity["notify"]()
            yield "完成"

        with patch.object(bot, "_send", side_effect=fake_send):
            await bot.stream_reply(
                _FakeUpdate(), deltas(), interval=0, activity_holder=activity,
                initial_delay=0, initial_chars=100,
            )

        self.assertIn("🔍 读取: file.py", calls)
        self.assertEqual(calls[-1], "完成")

    async def test_tool_finishes_before_first_refresh_without_blocking_loop(self) -> None:
        calls: list[str] = []
        activity = {"current": None}

        async def fake_send(send_fn, text, *, edit=False, msg=None, reply_markup=None):
            calls.append(text)
            return True if edit else _SentMessage()

        async def deltas():
            activity["current"] = "🔍 读取: file.py"
            activity["notify"]()
            activity["current"] = None
            activity["notify"]()
            await asyncio.sleep(0.02)
            yield "完成"

        with patch.object(bot, "_send", side_effect=fake_send):
            await asyncio.wait_for(
                bot.stream_reply(
                    _FakeUpdate(), deltas(), interval=0, activity_holder=activity,
                    initial_delay=0.001, initial_chars=100,
                ), timeout=0.2,
            )

        self.assertEqual(calls[-1], "完成")


class MarkdownSplitTests(unittest.TestCase):
    def test_long_single_line_is_bounded(self) -> None:
        text = "<" * 5000
        chunks = bot.split_markdown(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(bot.md_to_html(chunk)) <= bot.MAX_HTML_LEN for chunk in chunks))

    def test_long_code_block_is_bounded_and_keeps_code_markup(self) -> None:
        text = "```python\n" + "a" * 5000 + "\n```"
        chunks = bot.split_markdown(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith("```python\n") and chunk.endswith("\n```") for chunk in chunks))
        self.assertTrue(all(len(bot.md_to_html(chunk)) <= bot.MAX_HTML_LEN for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
