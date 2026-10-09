"""在线备份、验证与每日维护；不读取或归档 .env。"""

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath


def _private(path: Path, mode: int) -> None:
    if os.name == "posix":
        path.chmod(mode)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _write_json(path: Path, value: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    _private(temp, 0o600)
    os.replace(temp, path)


def backup_status(settings) -> dict:
    path = Path(settings.backup_root) / "last-backup.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["age_seconds"] = round(max(0, time.time() - value["timestamp"]), 1)
        value["stale"] = value["age_seconds"] > settings.backup_interval_seconds * 1.1
        return {"enabled": settings.backup_enabled, **value}
    except (OSError, ValueError, KeyError, TypeError):
        return {"enabled": settings.backup_enabled, "stale": True, "archive": None}


def create_backup(db_path: str, session_dir: str, backup_root: str, *, reason: str = "scheduled") -> dict:
    """调用者先等会话空闲；独立连接取得一致 DB，再归档完整 JSONL。"""
    source = Path(db_path).resolve()
    if not source.is_file():
        raise FileNotFoundError("备份源数据库不存在")
    root = Path(backup_root).resolve()
    daily, weekly = root / "daily", root / "weekly"
    for folder in (root, daily, weekly):
        folder.mkdir(parents=True, exist_ok=True)
        _private(folder, 0o700)
    now = datetime.now(timezone.utc)
    name = "backup-" + now.strftime("%Y%m%dT%H%M%S.%fZ") + ".tar.gz"
    archive = daily / name
    temp_archive = archive.with_name("." + name + ".tmp")
    try:
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=root) as folder:
            stage = Path(folder)
            _private(stage, 0o700)
            snapshot = stage / "bot.db"
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(snapshot)) as dst:
                    src.backup(dst)
                    if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份数据库完整性检查失败")
            _private(snapshot, 0o600)
            sessions = Path(session_dir).resolve()
            files = [snapshot]
            if sessions.is_dir():
                for item in sorted(sessions.rglob("*.jsonl")):
                    if item.is_symlink() or not item.is_file():
                        continue
                    relative = item.relative_to(sessions)
                    target = stage / "pi-sessions" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    before = item.stat()
                    shutil.copyfile(item, target)
                    after = item.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise RuntimeError("会话仍在写入，稍后重新备份")
                    with target.open("rb") as stream:
                        for line in stream:
                            if line.strip():
                                json.loads(line)
                    files.append(target)
            manifest = {
                "version": 1, "timestamp": now.timestamp(), "created_at": now.isoformat(),
                "reason": reason, "db_integrity": "ok", "session_files": len(files) - 1,
                "source_session_dir": str(sessions),
                "files": [{"path": str(item.relative_to(stage).as_posix()),
                           "bytes": item.stat().st_size, "sha256": _digest(item)} for item in files],
            }
            manifest["total_bytes"] = sum(item["bytes"] for item in manifest["files"])
            _write_json(stage / "manifest.json", manifest)
            with tarfile.open(temp_archive, "w:gz") as tar:
                for item in [stage / "manifest.json", *files]:
                    tar.add(item, arcname=item.relative_to(stage).as_posix(), recursive=False)
            _private(temp_archive, 0o600)
            os.replace(temp_archive, archive)
        # 每个 ISO 周保留第一份完整快照，日备份和周备份互相独立。
        week_key = now.strftime("%G-W%V")
        if not list(weekly.glob(week_key + "-*.tar.gz")):
            weekly_archive = weekly / (week_key + "-" + name)
            shutil.copyfile(archive, weekly_archive)
            _private(weekly_archive, 0o600)
        for folder, limit in ((daily, 7), (weekly, 4)):
            for old in sorted(folder.glob("*.tar.gz"), reverse=True)[limit:]:
                old.unlink()
        status = {key: manifest[key] for key in ("timestamp", "created_at", "db_integrity", "session_files")}
        status.update(archive=str(archive), size_bytes=archive.stat().st_size, reason=reason, error=None)
        _write_json(root / "last-backup.json", status)
        return status
    finally:
        temp_archive.unlink(missing_ok=True)


def verify_backup(archive_path: str, destination: str | None = None) -> dict:
    """严格验证到临时目录；恢复只允许全新的目标目录，不修改线上文件。"""
    destination_path = Path(destination).resolve() if destination else None
    if destination_path is not None and destination_path.exists():
        raise ValueError("恢复目标必须是不存在的新目录")
    parent = destination_path.parent if destination_path else Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=parent) as folder:
        stage = Path(folder) / "data"
        stage.mkdir()
        _private(stage, 0o700)
        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)) or names.count("manifest.json") != 1:
                raise ValueError("备份成员重复或缺少 manifest")
            manifest_member = tar.getmember("manifest.json")
            if not manifest_member.isfile() or manifest_member.size > 1024 * 1024:
                raise ValueError("备份 manifest 无效或过大")
            manifest = json.load(tar.extractfile(manifest_member))
            if manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
                raise ValueError("备份格式不支持")
            created_at = datetime.fromisoformat(manifest.get("created_at", ""))
            if (created_at.utcoffset() is None or created_at.utcoffset().total_seconds() != 0
                    or not isinstance(manifest.get("timestamp"), (int, float))
                    or not math.isfinite(manifest["timestamp"])
                    or abs(created_at.timestamp() - manifest["timestamp"]) > 1):
                raise ValueError("备份 UTC 时间无效")
            declared = {}
            for item in manifest["files"]:
                name = item["path"]
                path = PurePosixPath(name)
                if (not name or "\\" in name or ":" in name or path.is_absolute()
                        or ".." in path.parts or str(path) != name or name in declared
                        or not (name == "bot.db" or name.startswith("pi-sessions/"))
                        or type(item["bytes"]) is not int or item["bytes"] < 0
                        or not re.fullmatch(r"[a-f0-9]{64}", item.get("sha256", ""))):
                    raise ValueError("备份包含不安全的路径或大小")
                declared[name] = item
            if "bot.db" not in declared or set(names) != set(declared) | {"manifest.json"}:
                raise ValueError("备份文件清单不匹配")
            total = sum(item["bytes"] for item in declared.values())
            if total != manifest.get("total_bytes") or total > 10 * 1024**3:
                raise ValueError("备份总大小无效或超过 10 GiB")
            for member in members:
                if member.name == "manifest.json":
                    continue
                item = declared[member.name]
                if not member.isfile() or member.size != item["bytes"]:
                    raise ValueError("备份文件类型或大小不匹配")
                target = stage.joinpath(*PurePosixPath(member.name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open("wb") as dst:
                    remaining = member.size
                    while remaining:
                        chunk = src.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("备份文件被截断")
                        dst.write(chunk)
                        remaining -= len(chunk)
                _private(target, 0o600)
                if _digest(target) != item["sha256"]:
                    raise ValueError("备份文件校验失败")
                if member.name.endswith(".jsonl"):
                    with target.open("rb") as stream:
                        for line in stream:
                            if line.strip():
                                json.loads(line)
        with closing(sqlite3.connect((stage / "bot.db").as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("恢复数据库完整性检查失败")
        _write_json(stage / "manifest.json", manifest)
        if manifest.get("source_session_dir") is not None and not isinstance(manifest["source_session_dir"], str):
            raise ValueError("备份原会话目录无效")
        restore = {"manual_rebind": not bool(manifest.get("source_session_dir")),
                   "rebound_paths": 0, "database_changed": False}
        if destination_path is not None:
            source_root = manifest.get("source_session_dir")
            if isinstance(source_root, str) and source_root:
                path_type = PureWindowsPath if re.match(r"^[A-Za-z]:[\\/]", source_root) else PurePosixPath
                original_root = path_type(source_root)
                if not original_root.is_absolute():
                    raise ValueError("原会话目录必须是绝对路径")
                with closing(sqlite3.connect(stage / "bot.db")) as db, db:
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table, key in (("sessions", "id"), ("session_recovery", "session_id")):
                        if table not in tables:
                            continue
                        for ident, old in db.execute(f"SELECT {key}, pi_session_file FROM {table}").fetchall():
                            if not old:
                                continue
                            try:
                                relative = path_type(old).relative_to(original_root)
                            except ValueError:
                                restore["manual_rebind"] = True
                                continue
                            name = "pi-sessions/" + relative.as_posix()
                            if name not in declared or ".." in relative.parts:
                                restore["manual_rebind"] = True
                                continue
                            new = str(destination_path.joinpath("pi-sessions", *relative.parts))
                            db.execute(f"UPDATE {table} SET pi_session_file = ? WHERE {key} = ?", (new, ident))
                            restore["rebound_paths"] += 1
                restore["database_changed"] = bool(restore["rebound_paths"])
            restore.update(timestamp=time.time(), source_database_sha256=declared["bot.db"]["sha256"],
                           restored_database_sha256=_digest(stage / "bot.db"))
            _write_json(stage / "restore-metadata.json", restore)
            # 同一文件系统中先完成全部验证，再一次性公布新恢复目录。
            os.rename(stage, destination_path)
        manifest["restore"] = restore
        return manifest
