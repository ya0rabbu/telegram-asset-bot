"""Secure password and PIN generation."""

from __future__ import annotations
import secrets
import string

AMBIGUOUS_CHARS  = "il1Lo0O"
LOWER            = string.ascii_lowercase
UPPER            = string.ascii_uppercase
DIGITS           = string.digits
SYMBOLS          = "!@#$%^&*()-_=+[]{};:,.?/"
MIN_LENGTH       = 4
MAX_LENGTH       = 128
DEFAULT_LENGTH   = 16


def generate_password(
    length: int = DEFAULT_LENGTH,
    use_upper: bool = True,
    use_lower: bool = True,
    use_digits: bool = True,
    use_symbols: bool = True,
    no_ambiguous: bool = False,
) -> str:
    length = max(MIN_LENGTH, min(MAX_LENGTH, length))
    pools = [
        p for flag, p in (
            (use_lower,   LOWER),
            (use_upper,   UPPER),
            (use_digits,  DIGITS),
            (use_symbols, SYMBOLS),
        ) if flag
    ] or [LOWER, DIGITS]

    if no_ambiguous:
        pools = [
            "".join(c for c in p if c not in AMBIGUOUS_CHARS) or p
            for p in pools
        ]

    alphabet = "".join(pools)
    chars = (
        [secrets.choice(p) for p in pools]
        + [secrets.choice(alphabet) for _ in range(length - len(pools))]
    )
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars[:length])


def generate_pin(length: int = 4) -> str:
    return "".join(
        secrets.choice(string.digits)
        for _ in range(max(3, min(12, length)))
    )
