import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import mail_cli
from storage import Storage


class MailConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "bot.db")
        self.storage = Storage(self.db_path)
        self.storage.save_smtp_settings(
            "mail.example.com", 465, "bot@example.com", "test-secret", "bot@example.com",
        )
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            message=self.message, callback_query=None,
        )
        self.context = SimpleNamespace(args=[], bot_data={"storage": self.storage})

    async def asyncTearDown(self):
        self.storage._conn.close()
        self.temp.cleanup()

    async def test_mail_cli_stages_without_smtp(self):
        request = {"to": "owner@example.com", "subject": "测试", "body": "正文"}
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))
        output = io.StringIO()
        with patch.dict(os.environ, {
            "DJHX_MAIL_DB_PATH": self.db_path,
            "DJHX_MAIL_USER_ID": "7",
        }):
            with patch.object(mail_cli.sys, "stdin", stdin):
                with patch("mail_service.smtplib.SMTP_SSL") as smtp:
                    with redirect_stdout(output):
                        code = mail_cli.main()
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.assertEqual(len(self.storage.list_pending_mail_drafts(7)), 1)
        smtp.assert_not_called()

    async def test_preview_is_exact_plain_text_and_buttons_follow_last_part(self):
        body = "<test>&" * 800
        draft_id = self.storage.create_mail_draft(7, "owner@example.com", "主题", body)
        await bot._mail_preview(
            self.update, self.storage, self.storage.get_mail_draft(7, draft_id),
        )
        calls = self.message.reply_text.await_args_list
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(call.kwargs["parse_mode"] is None for call in calls))
        self.assertTrue(all(call.kwargs["reply_markup"] is None for call in calls[:-1]))
        self.assertIsNotNone(calls[-1].kwargs["reply_markup"])
        joined = "".join(call.args[0] for call in calls)
        self.assertIn("收件人：owner@example.com", joined)
        self.assertIn("主题：主题", joined)
        self.assertIn(body, joined)
        self.assertIsNotNone(self.storage.get_mail_draft(7, draft_id)["previewed_at"])

    async def test_confirmation_sends_once_and_cancellation_never_sends(self):
        first = self.storage.create_mail_draft(7, "owner@example.com", "测试", "正文")
        self.storage.mark_mail_previewed(7, first)
        self.context.args = ["confirm", first]
        with patch.object(bot, "reply_text", new_callable=AsyncMock):
            with patch.object(bot, "send_email") as send:
                await bot.cmd_mail(self.update, self.context)
                await bot.cmd_mail(self.update, self.context)
        send.assert_called_once()
        self.assertEqual(self.storage.get_mail_draft(7, first)["state"], "sent")

        second = self.storage.create_mail_draft(7, "owner@example.com", "取消", "正文")
        self.context.args = ["cancel", second]
        with patch.object(bot, "reply_text", new_callable=AsyncMock):
            with patch.object(bot, "send_email") as send:
                await bot.cmd_mail(self.update, self.context)
        send.assert_not_called()
        self.assertEqual(self.storage.get_mail_draft(7, second)["state"], "cancelled")

    async def test_unpreviewed_or_expired_draft_cannot_send(self):
        draft_id = self.storage.create_mail_draft(7, "owner@example.com", "测试", "正文")
        self.context.args = ["confirm", draft_id]
        with patch.object(bot, "reply_text", new_callable=AsyncMock):
            with patch.object(bot, "send_email") as send:
                await bot.cmd_mail(self.update, self.context)
        send.assert_not_called()
        self.assertIsNotNone(self.storage.get_mail_draft(7, draft_id)["previewed_at"])

        self.storage._exec(
            "UPDATE mail_drafts SET created_at = 1 WHERE id = ?", (draft_id,),
        )
        self.assertFalse(self.storage.claim_mail_draft(7, draft_id))
        self.assertEqual(self.storage.get_mail_draft(7, draft_id)["state"], "pending")


if __name__ == "__main__":
    unittest.main()
