"""Developer utility tools."""

from __future__ import annotations
import base64
import datetime
import hashlib
import json
import re
import uuid
from urllib.parse import quote, unquote


_LOREM_WORDS = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua enim ad minim veniam quis nostrud "
    "exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat duis aute "
    "irure dolor in reprehenderit voluptate velit esse cillum dolore eu fugiat nulla "
    "pariatur excepteur sint occaecat cupidatat non proident sunt in culpa qui officia "
    "deserunt mollit anim id est laborum".split()
)


def format_json(text: str) -> str:
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except json.JSONDecodeError as exc:
        return f"❌ Invalid JSON: {exc}"


def hash_text(text: str) -> str:
    encoded = text.encode("utf-8")
    return (
        f"MD5:    `{hashlib.md5(encoded).hexdigest()}`\n"
        f"SHA1:   `{hashlib.sha1(encoded).hexdigest()}`\n"
        f"SHA256: `{hashlib.sha256(encoded).hexdigest()}`\n"
        f"SHA512: `{hashlib.sha512(encoded).hexdigest()[:64]}…`"
    )


def generate_uuid() -> str:
    u = uuid.uuid4()
    return (
        f"UUID v4:   `{u}`\n"
        f"No dashes: `{u.hex}`\n"
        f"URN:       `urn:uuid:{u}`"
    )


def convert_timestamp(value: str) -> str:
    value = value.strip()
    try:
        ts = float(value)
        dt = datetime.datetime.utcfromtimestamp(ts)
        return (
            f"Unix: `{int(ts)}`\n"
            f"UTC:  `{dt.strftime('%Y-%m-%d %H:%M:%S')} UTC`\n"
            f"ISO:  `{dt.isoformat()}Z`"
        )
    except ValueError:
        pass
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            f"Unix: `{int(dt.timestamp())}`\n"
            f"UTC:  `{dt.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`\n"
            f"ISO:  `{dt.isoformat()}`"
        )
    except ValueError:
        return "❌ Provide a Unix timestamp or ISO date."


def lorem_ipsum(n_words: int = 50) -> str:
    import random
    words    = [random.choice(_LOREM_WORDS) for _ in range(n_words)]  # noqa: S311
    words[0] = words[0].capitalize()
    return " ".join(words) + "."


def convert_case(text: str, mode: str) -> str:
    if mode == "camel":
        words = re.split(r"[\s_\-]+", text)
        return words[0].lower() + "".join(w.capitalize() for w in words[1:])
    if mode == "pascal":
        return "".join(w.capitalize() for w in re.split(r"[\s_\-]+", text))
    if mode == "snake":
        s = re.sub(r"(?<!^)(?=[A-Z])", "_", text).lower()
        return re.sub(r"[\s\-]+", "_", s)
    if mode == "kebab":
        s = re.sub(r"(?<!^)(?=[A-Z])", "-", text).lower()
        return re.sub(r"[\s_]+", "-", s)
    if mode == "upper":  return text.upper()
    if mode == "lower":  return text.lower()
    if mode == "title":  return text.title()
    return text


def px_to_rem(px: float, base: float = 16.0) -> str:
    return (
        f"`{px}px` = `{px/base:.4f}rem` (base {base}px)\n"
        f"  16px base → `{px/16:.4f}rem`\n"
        f"  14px base → `{px/14:.4f}rem`\n"
        f"  18px base → `{px/18:.4f}rem`"
    )


def url_encode(text: str) -> str:
    return quote(text, safe="")


def url_decode(text: str) -> str:
    return unquote(text)


def base64_encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def base64_decode(text: str) -> str:
    try:
        return base64.b64decode(text.encode("ascii")).decode("utf-8")
    except Exception:
        return "❌ Invalid base64 string."


def word_count(text: str) -> str:
    words          = text.split()
    chars          = len(text)
    chars_no_space = len(text.replace(" ", ""))
    lines          = text.count("\n") + 1
    read_time      = max(1, round(len(words) / 200))
    return (
        f"Words:         `{len(words)}`\n"
        f"Characters:    `{chars}`\n"
        f"Chars (no sp): `{chars_no_space}`\n"
        f"Lines:         `{lines}`\n"
        f"Reading time:  ~{read_time} min"
    )


def diff_texts(text_a: str, text_b: str) -> str:
    import difflib
    diff = list(difflib.unified_diff(
        text_a.splitlines(keepends=True),
        text_b.splitlines(keepends=True),
        fromfile="Text A", tofile="Text B", lineterm="",
    ))
    if not diff:
        return "✅ Texts are identical."
    result = "".join(diff[:80])
    if len(diff) > 80:
        result += f"\n… ({len(diff) - 80} more lines)"
    return result


def regex_test(pattern: str, text: str) -> str:
    try:
        rx      = re.compile(pattern)
        matches = list(rx.finditer(text))
        if not matches:
            return f"No matches for `{pattern}`."
        lines = [f"Found {len(matches)} match(es):"]
        for i, m in enumerate(matches[:10], 1):
            lines.append(f"  {i}. `{m.group()}` at [{m.start()}:{m.end()}]")
        return "\n".join(lines)
    except re.error as exc:
        return f"❌ Regex error: {exc}"
