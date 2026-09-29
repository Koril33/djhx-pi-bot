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

    async def test_idle_period_shows_elapsed_phase_then_clears_on_text(self) -> None:
        calls = []

        async def fake_send(send_fn, text, *, edit=False, msg=None, reply_markup=None):
            calls.append(text)
            return True if edit else _SentMessage()

        async def deltas():
            await asyncio.sleep(0.08)
            yield "最终回复"

        with patch.object(bot, "_send", side_effect=fake_send):
            await asyncio.wait_for(bot.stream_reply(
                _FakeUpdate(), deltas(), interval=0,
                initial_delay=0, idle_interval=0.02,
            ), timeout=1)
        self.assertTrue(any("等待模型响应" in call and "已进行" in call for call in calls))
        self.assertEqual(calls[-1], "最终回复")

    async def test_idle_tool_phase_uses_generic_label(self) -> None:
        calls = []
        activity = {"current": None}

        async def fake_send(send_fn, text, *, edit=False, msg=None, reply_markup=None):
            calls.append(text)
            return True if edit else _SentMessage()

        async def deltas():
            activity["current"] = "🔧 正在调用工具"
            activity["notify"]()
            await asyncio.sleep(0.07)
            activity["current"] = None
            activity["notify"]()
            yield "完成"

        with patch.object(bot, "_send", side_effect=fake_send):
            await bot.stream_reply(
                _FakeUpdate(), deltas(), interval=0,
                initial_delay=0, idle_interval=0.02, activity_holder=activity,
            )
        self.assertTrue(any("等待工具完成" in call for call in calls))
        self.assertEqual(calls[-1], "完成")

    async def test_tool_only_turn_has_clear_completion(self) -> None:
        calls = []

        async def fake_send(send_fn, text, *, edit=False, msg=None, reply_markup=None):
            calls.append(text)
            return True if edit else _SentMessage()

        async def deltas():
            if False:
                yield ""

        with patch.object(bot, "_send", side_effect=fake_send):
            await bot.stream_reply(
                _FakeUpdate(), deltas(), interval=0,
                activity_holder={"current": None, "completed": 1},
                placeholder={"msg": _SentMessage()},
            )
        self.assertEqual(calls, ["✅ 工具操作已完成；本轮没有文字回复。"])

    async def test_failed_tool_is_not_reported_as_success(self) -> None:
        calls = []

        async def fake_send(send_fn, text, *, edit=False, msg=None, reply_markup=None):
            calls.append(text)
            return True if edit else _SentMessage()

        async def deltas():
            if False:
                yield ""

        with patch.object(bot, "_send", side_effect=fake_send):
            await bot.stream_reply(
                _FakeUpdate(), deltas(), interval=0,
                activity_holder={"current": None, "completed": 1, "failed": 1},
            )
        self.assertIn("出现失败", calls[-1])
        self.assertNotIn("✅", calls[-1])

    async def test_empty_turn_without_tool_has_retry_guidance(self) -> None:
        calls = []

        async def fake_send(send_fn, text, *, edit=False, msg=None, reply_markup=None):
            calls.append(text)
            return True if edit else _SentMessage()

        async def deltas():
            if False:
                yield ""

        with patch.object(bot, "_send", side_effect=fake_send):
            await bot.stream_reply(_FakeUpdate(), deltas(), interval=0)
        self.assertEqual(calls, ["⚠️ 本轮没有文字回复，请重试或换个说法。"])

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
    def test_table_becomes_readable_list_without_code_block(self) -> None:
        table = (
            "问题汇总\n\n| # | 问题 | 位置 | 影响 |\n"
            "|---|---|---|---|\n"
            "| 1 | running 回合未收尾 | `storage.py:274` | 迁移时缺一轮 |\n"
            "| 2 | 记忆费用说明不清 | `bot.py:1110` | 容易误解 |"
        )
        html = bot.md_to_html(table)
        self.assertNotIn("<pre>", html)
        self.assertIn("问题汇总", html)
        self.assertIn("<b>1. running 回合未收尾</b>", html)
        self.assertNotIn("第 1 列：#", html)
        self.assertIn("问题：running 回合未收尾", html)
        self.assertIn("位置：<code>storage.py:274</code>", html)
        self.assertIn("<b>2. 记忆费用说明不清</b>", html)

    def test_table_inside_code_fence_stays_code(self) -> None:
        html = bot.md_to_html("```md\n| A | B |\n|---|---|\n| 1 | 2 |\n```")
        self.assertIn("<pre", html)
        self.assertNotIn("第 1 项", html)

    def test_code_language_uses_telegram_supported_html(self) -> None:
        html = bot.md_to_html("```python\nprint(1)\n```")
        self.assertEqual(html, '<pre><code class="language-python">print(1)</code></pre>')

    def test_link_target_is_escaped_and_plain_fallback_keeps_url(self) -> None:
        source = '[说明](https://example.com/path?q="a"&x=1)'
        html = bot.md_to_html(source)
        self.assertIn('href="https://example.com/path?q=&quot;a&quot;&amp;x=1"', html)
        self.assertEqual(
            bot.strip_markdown(source),
            '说明 (https://example.com/path?q="a"&x=1)',
        )

    def test_long_line_does_not_split_valid_link(self) -> None:
        link = "[参考资料](https://example.com/path?q=one&x=two)"
        text = "A" * 3860 + " " + link + " " + "Z" * 100
        chunks = bot.split_markdown(text)
        self.assertEqual("".join(chunks), text)
        self.assertEqual(sum(chunk.count(link) for chunk in chunks), 1)
        self.assertTrue(any("<a href=" in bot.md_to_html(chunk) for chunk in chunks))
        self.assertTrue(all(len(bot.md_to_html(chunk)) <= bot.MAX_HTML_LEN for chunk in chunks))

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
