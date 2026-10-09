"""小规模记忆整理：只提出候选，修改必须经过用户确认。"""

import hashlib
import json
import re
from difflib import SequenceMatcher


_SECRET = re.compile(
    r"(?i)(password|passwd|api[_ -]?key|access[_ -]?token|secret|密码|密钥|令牌|验证码)\s*[:=：]\s*\S+"
)
_ALIASES = (
    ("不喜欢", "不偏好"), ("讨厌", "不偏好"), ("喜欢", "偏好"),
    ("简短", "简洁"), ("精简", "简洁"), ("短一点", "简洁"),
    ("详细", "细致"), ("展开说明", "细致"), ("回复", "回答"),
    ("偏爱", "偏好"), ("使用", "用"),
)


def memory_version(rows) -> str:
    values = sorted((row["id"], row["text"], bool(row["pinned"]), row["updated_at"])
                    for row in rows)
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:16]


def validate_merge_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if (not text or len(text) > max_chars or _SECRET.search(text)
            or any(ord(char) < 32 and char not in "\n\t" for char in text)):
        raise ValueError("整理后的记忆为空、过长或包含敏感凭据")
    return text


def _normalized(text: str) -> str:
    value = text.casefold()
    for old, new in _ALIASES:
        value = value.replace(old, new)
    value = re.sub(r"用户|本人|我的|我|的|请|希望|倾向于", "", value)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value)


def _pair_reason(first: str, second: str) -> tuple[str, str, float] | None:
    left, right = _normalized(first), _normalized(second)
    if min(len(left), len(right)) < 4:
        return None
    # 这些都是供人工核对的启发式，不代表已经证明两条陈述矛盾。
    polarity = lambda value: bool(re.search(r"不|不要|从不|不能|避免|拒绝", value))
    skeleton = lambda value: re.sub(r"不要|从不|不能|避免|拒绝|不|\d+", "", value)
    left_core, right_core = skeleton(left), skeleton(right)
    core_ratio = SequenceMatcher(None, left_core, right_core, autojunk=False).ratio()
    ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
    numbers_differ = re.findall(r"\d+", left) != re.findall(r"\d+", right)
    style_conflict = ({"简洁", "细致"}.issubset(set(re.findall(r"简洁|细致", left + right)))
                      and "回答" in left and "回答" in right)
    if ((core_ratio >= .84 and min(len(left_core), len(right_core)) >= 4
         and (polarity(left) != polarity(right) or numbers_differ)) or style_conflict):
        return "possible_conflict", "相同主题的否定、数值或回答风格不同，请核对新旧偏好", max(ratio, core_ratio)
    if ratio >= .78 or (min(len(left), len(right)) >= 7 and (left in right or right in left)):
        return "near_duplicate", "措辞相近或表达同一偏好，可核对后合并", ratio
    return None


def organization_candidates(rows, limit: int = 10) -> list[dict]:
    """最多100条的本地两两比较；不自动删除、不调用模型。"""
    rows = list(rows)
    result = []
    for index, first in enumerate(rows):
        for second in rows[index + 1:]:
            match = _pair_reason(first["text"], second["text"])
            if match is None:
                continue
            kind, reason, score = match
            pair = [first, second]
            newest = max(pair, key=lambda row: (row["updated_at"], row["id"]))
            result.append({
                "kind": kind, "reason": reason, "score": round(score, 3),
                "ids": [first["id"], second["id"]],
                "rows": [dict(row) for row in pair], "version": memory_version(pair),
                "suggested_text": newest["text"],
                "pinned": any(row["pinned"] for row in pair),
            })
    result.sort(key=lambda row: (row["kind"] == "possible_conflict", row["score"]), reverse=True)
    return result[:max(0, limit)]
