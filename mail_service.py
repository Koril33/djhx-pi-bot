"""SMTP 配置校验与纯文本邮件发送。凭据只从服务端 SQLite 读取。"""

from email.message import EmailMessage
from email.utils import parseaddr
import re
import smtplib
import ssl


class MailSendError(Exception):
    """可安全展示给管理员或 Pi 的发信错误。"""


def email_address(value: str) -> str:
    address = value.strip()
    if (not 3 <= len(address) <= 254 or not address.isascii()
            or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in address)
            or parseaddr(address)[1] != address or address.count("@") != 1
            or not address.split("@", 1)[1].count(".")):
        raise ValueError("邮箱地址无效")
    return address


def smtp_settings(host: str, port: int | str, username: str,
                  password: str, from_address: str) -> dict[str, str | int]:
    host = host.strip().lower()
    username = username.strip()
    if not 1 <= len(host) <= 253 or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("SMTP 服务器地址无效")
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("端口仅支持 465 或 587") from exc
    if port not in (465, 587):
        raise ValueError("端口仅支持 465 或 587")
    if (not 1 <= len(username) <= 254 or any(ord(char) < 33 or ord(char) == 127 for char in username)):
        raise ValueError("SMTP 用户名无效")
    if not 1 <= len(password) <= 1024 or any(ord(char) < 32 or ord(char) == 127 for char in password):
        raise ValueError("SMTP 密码或授权码无效")
    return {"host": host, "port": port, "username": username,
            "password": password, "from_address": email_address(from_address)}


def send_email(config, to: str, subject: str, body: str) -> None:
    recipient = email_address(to)
    if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 200 or any(
        char in "\r\n\x00" for char in subject
    ):
        raise ValueError("邮件主题须为 1–200 个字符，不能包含换行")
    if not isinstance(body, str) or not 1 <= len(body) <= 20000 or "\x00" in body:
        raise ValueError("邮件正文须为 1–20000 个字符")

    message = EmailMessage()
    message["From"] = email_address(config["from_address"])
    message["To"] = recipient
    message["Subject"] = subject.strip()
    message.set_content(body)
    context = ssl.create_default_context()
    try:
        if config["port"] == 465:
            with smtplib.SMTP_SSL(config["host"], 465, timeout=20, context=context) as client:
                client.login(config["username"], config["password"])
                refused = client.send_message(message)
        elif config["port"] == 587:
            with smtplib.SMTP(config["host"], 587, timeout=20) as client:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
                client.login(config["username"], config["password"])
                refused = client.send_message(message)
        else:
            raise ValueError("端口仅支持 465 或 587")
        if refused:
            raise MailSendError("收件服务器拒收邮件")
    except smtplib.SMTPAuthenticationError as exc:
        raise MailSendError("SMTP 认证失败，请检查用户名和授权码") from exc
    except smtplib.SMTPNotSupportedError as exc:
        raise MailSendError("SMTP 服务器不支持所选端口的 TLS 协议") from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailSendError("SMTP 发送失败，请检查服务器、端口和证书") from exc
