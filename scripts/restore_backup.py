"""验证备份或恢复到新的离线目录；不会覆盖线上数据库。"""
import argparse
import json

from ops import verify_backup


def main() -> None:
    parser = argparse.ArgumentParser(description="校验备份并恢复到新目录")
    parser.add_argument("archive")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--verify-only", action="store_true")
    choice.add_argument("--destination")
    args = parser.parse_args()
    print(json.dumps(verify_backup(args.archive, args.destination), ensure_ascii=False))


if __name__ == "__main__":
    main()
