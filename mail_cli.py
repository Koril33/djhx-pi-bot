"""Pi 邮件工具的窄接口：只保存待确认草稿，不直接连接 SMTP。"""

import json
import os
from pathlib import Path
import sys

from mail_service import MailSendError, validate_email_request
from storage import Storage


def main() -> int:
    try:
        db_path = Path(os.environ["DJHX_MAIL_DB_PATH"])
        if not db_path.is_absolute() or not db_path.is_file():
            raise MailSendError("邮件配置不可用")
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("邮件内容过长")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {"to", "subject", "body"}:
            raise ValueError("邮件参数无效")
        user_id = int(os.environ["DJHX_MAIL_USER_ID"])
        if user_id <= 0:
            raise ValueError("邮件用户无效")
        recipient, subject, body = validate_email_request(
            request["to"], request["subject"], request["body"],
        )
        storage = Storage(str(db_path))
        try:
            if storage.get_smtp_settings() is None:
                raise MailSendError("请先在管理后台配置 SMTP")
            draft_id = storage.create_mail_draft(user_id, recipient, subject, body)
        finally:
            storage._conn.close()
        result = {"ok": True, "message": "邮件草稿已创建，请在 Telegram 预览后确认发送", "draft_id": draft_id}
        code = 0
    except (KeyError, TypeError, ValueError, MailSendError, json.JSONDecodeError) as exc:
        result = {"ok": False, "message": str(exc)}
        code = 1
    except Exception:
        result = {"ok": False, "message": "创建草稿失败，请检查服务日志"}
        code = 1
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
