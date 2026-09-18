"""Fiver message sanitizer v3."""

from __future__ import annotations
import re
from modules.ui.strings import (
    FIVER_TEMPLATE,
    DEFAULT_CLOSING_LINE,
    FIVER_INFO_PATTERN_HINT,
)

_AI_SIGNS = re.compile(
    r"[\u2014\u2013\u2012\u2015]"
    r"|[\u2018\u2019\u201A\u201B]"
    r"|[\u201C\u201D\u201E\u201F]"
    r"|[\u2026]"
    r"|(?<!\w)_(?!\w)"
    r"|(?<=\s)_|_(?=\s)",
    re.UNICODE,
)

FIVER_WORD_MAP: dict[str, str] = {
    "email": "ema-il", "gmail": "gma-il", "whatsapp": "wha-tsapp",
    "skype": "sky-pe", "telegram": "tele-gram", "discord": "dis-cord",
    "phone": "pho-ne", "mobile": "mobi-le", "number": "num-ber",
    "contact": "conta-ct", "zoom": "zo-om", "slack": "sla-ck",
    "linkedin": "link-edin", "facebook": "face-book", "instagram": "inst-agram",
    "twitter": "twit-ter", "youtube": "yout-ube", "tiktok": "tik-tok",
    "snapchat": "snap-chat", "pinterest": "pint-erest", "reddit": "red-dit",
    "meeting": "mee-ting", "call": "ca-ll", "anydesk": "any-desk",
    "teamviewer": "team-viewer", "payment": "pa-yment", "paypal": "p-ay-pal",
    "pay": "p-ay", "payoneer": "p-ay-oneer", "crypto": "cry-pto",
    "bitcoin": "bit-coin", "wire": "wi-re", "bank": "ba-nk",
    "transfer": "trans-fer", "cash": "ca-sh", "invoice": "invo-ice",
    "outside": "outsi-de", "direct": "dire-ct", "stripe": "str-ipe",
    "fee": "f-ee", "price": "pri-ce", "cost": "co-st", "billing": "bill-ing",
    "homework": "home-work", "assignment": "assign-ment", "essay": "ess-ay",
    "thesis": "the-sis", "exam": "ex-am", "test": "te-st",
    "review": "revi-ew", "feedback": "feed-back", "rating": "rat-ing",
    "password": "pass-word", "login": "lo-gin", "address": "addr-ess",
    "guaranteed": "guaran-teed", "money": "mon-ey", "income": "inco-me",
    "profit": "pro-fit", "fiverr": "fiv-err", "order": "ord-er",
    "cancel": "can-cel", "refund": "refu-nd",
}

GREETING_NORMALIZE_RE = re.compile(
    r"^\s*(hi+|hey+|hello+)\b[\s,!.]*", re.IGNORECASE,
)
SIGNOFF_RE = re.compile(
    r"\n{1,3}\s*(best\s*regards|regards|best|thanks\s*&?\s*regards"
    r"|warm\s*regards|sincerely)[\s,]*\n?.*$",
    re.IGNORECASE | re.DOTALL,
)
THANK_YOU_CHECK_CHARS = 250


def _preserve_case(original: str, replacement: str) -> str:
    if original.islower():      return replacement.lower()
    if original.isupper():      return replacement.upper()
    if original[:1].isupper():  return replacement[:1].upper() + replacement[1:].lower()
    return replacement


def _build_patterns(word_map: dict[str, str]) -> list[tuple[str, re.Pattern]]:
    return [
        (w, re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE))
        for w in sorted(word_map, key=len, reverse=True)
    ]


_FIVER_WORD_PATTERNS = _build_patterns(FIVER_WORD_MAP)


def markdown_to_plain_text(md: str) -> str:
    text = re.sub(r"!\[.*?\]\(.*?\)", "", md)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_(?!_)(.*?)_(?!_)", r"\1", text)
    text = re.sub(r"`{1,3}(.*?)`{1,3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_greeting(text: str) -> tuple[str, str | None]:
    m = GREETING_NORMALIZE_RE.match(text)
    if not m:
        return text, None
    original = m.group(0).strip().rstrip(",.! ")
    new_text = GREETING_NORMALIZE_RE.sub("Hello there,\n\n", text, count=1)
    return new_text, f"Greeting normalized: `{original}` → `Hello there,`"


def normalize_closing(text: str) -> tuple[str, str | None]:
    tail            = text[-THANK_YOU_CHECK_CHARS:]
    has_thanks      = "thank you" in tail.lower()
    signoff_match   = SIGNOFF_RE.search(text)
    if signoff_match:
        stripped = text[: signoff_match.start()].rstrip()
        return (
            f"{stripped}\n\n{DEFAULT_CLOSING_LINE}",
            "Sign-off removed and replaced with a thank-you closing line.",
        )
    if not has_thanks:
        return (
            f"{text.rstrip()}\n\n{DEFAULT_CLOSING_LINE}",
            "No thank-you found at the end — closing line added.",
        )
    return text, None


def parse_fiver_info(text: str) -> dict[str, str] | None:
    parts = [p.strip() for p in text.strip().split("_")]
    if len(parts) != 5 or not all(parts):
        return None
    client, order_id, project, profile, amount = parts
    if not amount.startswith("$"):
        amount = f"${amount}"
    return {
        "client": client, "order_id": order_id,
        "project": project, "profile": profile, "amount": amount,
    }


def sanitize_fiver_text(
    text: str,
    custom_words: dict[str, str] | None = None,
) -> tuple[str, list[str], int]:
    changes: list[str] = []

    plain = markdown_to_plain_text(text)
    if plain != text:
        changes.append("Markdown formatting removed")
    text = plain

    ai_stripped   = _AI_SIGNS.sub("", text)
    stripped_count = len(text) - len(ai_stripped)
    if stripped_count:
        changes.append(f"AI signs removed ({stripped_count} character(s))")
    result = ai_stripped

    merged   = {**FIVER_WORD_MAP, **(custom_words or {})}
    patterns = _build_patterns(merged) if custom_words else _FIVER_WORD_PATTERNS

    for word, pattern in patterns:
        replacement = merged[word]
        new_result, n = pattern.subn(
            lambda m: _preserve_case(m.group(0), replacement), result
        )
        if n:
            changes.append(f"`{word}` → `{replacement}` (×{n})")
        result = new_result

    result, greet_note = normalize_greeting(result)
    if greet_note:
        changes.append(greet_note)

    result, close_note = normalize_closing(result)
    if close_note:
        changes.append(close_note)

    score = max(0, 100 - len(changes) * 8)
    return result, changes, score


def build_fiver_output(info: dict[str, str], sanitized_body: str) -> str:
    return FIVER_TEMPLATE.format(
        profile=info["profile"],
        client=info["client"],
        project=info["project"],
        order_id=info["order_id"],
        amount=info["amount"],
        body=sanitized_body.strip(),
    )


def format_sanitizer_report(changes: list[str], score: int) -> str:
    quality_icon = "✅" if score >= 80 else ("⚠️" if score >= 50 else "❌")
    lines = [f"📊 *Quality Check:* {quality_icon} {score}/100", ""]
    if changes:
        lines.append("🔄 *What was changed:*")
        for c in changes[:15]:
            lines.append(f"  • {c}")
        if len(changes) > 15:
            lines.append(f"  … and {len(changes) - 15} more")
    else:
        lines.append("✅ No flagged words found — message is clean!")
    return "\n".join(lines)
