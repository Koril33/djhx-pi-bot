"""Pi 邮件工具的窄接口：stdin JSON，仅含收件人、主题、正文。"""

import json
import os
from pathlib import Path
import sys

from mail_service import MailSendError, send_email
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
        storage = Storage(str(db_path))
        try:
            config = storage.get_smtp_settings()
        finally:
            storage._conn.close()
        if config is None:
            raise MailSendError("请先在管理后台配置 SMTP")
        send_email(config, request["to"], request["subject"], request["body"])
        result = {"ok": True, "message": "邮件已提交给 SMTP 服务器", "to": request["to"]}
        code = 0
    except (KeyError, TypeError, ValueError, MailSendError, json.JSONDecodeError) as exc:
        result = {"ok": False, "message": str(exc)}
        code = 1
    except Exception:
        result = {"ok": False, "message": "发送失败，请检查服务日志"}
        code = 1
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
