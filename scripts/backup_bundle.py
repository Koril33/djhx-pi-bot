"""uv run python -m scripts.backup_bundle [--reason deploy]"""
import argparse
import json

from config import Settings
from ops import create_backup


def main() -> None:
    parser = argparse.ArgumentParser(description="生成数据库与完整 pi 会话备份")
    parser.add_argument("--reason", default="manual")
    args = parser.parse_args()
    settings = Settings()
    print(json.dumps(create_backup(settings.db_path, settings.pi_session_dir,
                                   settings.backup_root, reason=args.reason), ensure_ascii=False))


if __name__ == "__main__":
    main()
