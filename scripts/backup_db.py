"""生成可单独迁移的一致 SQLite 备份。运行：uv run python -m scripts.backup_db backup.db"""

import argparse
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="创建 SQLite 单文件在线备份")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", type=Path, default=Path(Settings().db_path))
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    if not source.is_file():
        parser.error(f"数据库不存在: {source}")
    if source == destination:
        parser.error("备份目标不能与原数据库相同")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".sqlite-backup-", dir=destination.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(temp_name)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("备份数据库完整性检查失败")
        if os.name == "posix":
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print(destination)


if __name__ == "__main__":
    main()
