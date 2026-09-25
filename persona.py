"""SQLite 人格的校验、提示词组装与 Pi 加载文件。"""

import hashlib
import json
import os
from pathlib import Path
import tempfile


FIELDS = (
    ("identity", "身份"),
    ("personality", "性格"),
    ("communication_style", "沟通风格"),
    ("behavior_rules", "行为规则"),
    ("background", "背景"),
    ("preferences", "偏好"),
)
DEFAULT_PERSONA = json.loads(
    (Path(__file__).resolve().parent / "prompts" / "default-persona.json").read_text(encoding="utf-8")
)


def validate_persona(values: dict[str, str]) -> dict[str, str]:
    name = values.get("name", "").strip()
    if not 1 <= len(name) <= 60 or any(ord(char) < 32 for char in name):
        raise ValueError("名称须为 1–60 个字符且不能换行")
    cleaned = {"name": name}
    for key, label in FIELDS:
        text = values.get(key, "").replace("\r\n", "\n").strip()
        if len(text) > 4000 or any(ord(char) < 32 and char not in "\n\t" for char in text):
            raise ValueError(f"{label}最多 4000 字符，不能包含控制字符")
        cleaned[key] = text
    if not cleaned["identity"]:
        raise ValueError("身份不能为空")
    if sum(len(cleaned[key]) for key, _ in FIELDS) > 12000:
        raise ValueError("人格正文总长度不能超过 12000 字符")
    return cleaned


def render_persona(row) -> str:
    """返回传给 Pi --append-system-prompt 的确切文本。"""
    parts = [f"# 人格：{row['name']}"]
    for key, label in FIELDS:
        value = row[key].strip()
        if value:
            parts.append(f"## {label}\n{value}")
    return "\n\n".join(parts) + "\n"


def persona_prompt_path(db_path: str, user_id: int) -> Path:
    return Path(db_path).resolve().parent / "persona-prompts" / f"{user_id}.md"


def write_prompt(path: Path, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return digest
    fd, temp_name = tempfile.mkstemp(prefix=".persona-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if os.name == "posix":
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return digest
