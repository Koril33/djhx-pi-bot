import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mail_service import MailSendError, send_email, smtp_settings
from storage import Storage


class MailTests(unittest.TestCase):
    def setUp(self):
        self.config = smtp_settings("mail.example.com", 465, "bot@example.com",
                                    "secret", "bot@example.com")

    def test_465_uses_implicit_tls(self):
        with patch("mail_service.smtplib.SMTP_SSL") as ssl_client, patch("mail_service.smtplib.SMTP") as plain_client:
            client = ssl_client.return_value.__enter__.return_value
            client.send_message.return_value = {}
            send_email(self.config, "admin@example.com", "测试", "正文")
        ssl_client.assert_called_once()
        self.assertEqual(ssl_client.call_args.args, ("mail.example.com", 465))
        client.login.assert_called_once_with("bot@example.com", "secret")
        client.starttls.assert_not_called()
        plain_client.assert_not_called()

    def test_587_upgrades_before_authentication(self):
        self.config["port"] = 587
        with patch("mail_service.smtplib.SMTP") as plain_client, patch("mail_service.smtplib.SMTP_SSL") as ssl_client:
            client = plain_client.return_value.__enter__.return_value
            client.send_message.return_value = {}
            calls = []
            for name in ("ehlo", "starttls", "login", "send_message"):
                getattr(client, name).side_effect = lambda *args, _name=name, **kwargs: calls.append(_name) or {}
            send_email(self.config, "admin@example.com", "Subject", "Body")
        self.assertEqual(calls, ["ehlo", "starttls", "ehlo", "login", "send_message"])
        plain_client.assert_called_once()
        ssl_client.assert_not_called()

    def test_rejects_header_injection_and_bad_port(self):
        with self.assertRaises(ValueError):
            send_email(self.config, "admin@example.com", "subject\nBcc: other@example.com", "Body")
        with self.assertRaises(ValueError):
            smtp_settings("mail.example.com", 25, "bot@example.com", "secret", "bot@example.com")

    def test_smtp_credentials_survive_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "bot.db")
            first = Storage(path)
            first.save_smtp_settings(**self.config)
            first._conn.close()
            second = Storage(path)
            row = second.get_smtp_settings()
            self.assertEqual(row["password"], "secret")
            self.assertEqual(row["port"], 465)
            second._conn.close()
