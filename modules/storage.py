"""Lightweight JSON-backed persistence layer."""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from typing import Deque

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(DATA_DIR, exist_ok=True)

CUSTOM_WORDS_PATH    = os.path.join(DATA_DIR, "custom_words.json")
USAGE_STATS_PATH     = os.path.join(DATA_DIR, "usage_stats.json")
USER_ACTIVITY_PATH   = os.path.join(DATA_DIR, "user_activity.json")
WATERMARKS_PATH      = os.path.join(DATA_DIR, "watermarks.json")
CUSTOM_LIMITS_PATH   = os.path.join(DATA_DIR, "custom_limits.json")
LIMIT_REQUESTS_PATH  = os.path.join(DATA_DIR, "limit_requests.json")
BLOCKED_USERS_PATH   = os.path.join(DATA_DIR, "blocked_users.json")
RATE_LIMIT_LOG_PATH  = os.path.join(DATA_DIR, "rate_limit_log.json")

MAX_WORDS_PER_USER        = 100
RESERVED_WORDS            = {"fiverr"}
MAX_LOG_ENTRIES_PER_USER  = 50
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_HEAVY_MAX_CALLS = 5

import asyncio
HEAVY_JOB_SEMAPHORE = asyncio.Semaphore(3)

HEAVY_TOOLS = {
    "bgremove", "effects", "pptx2images", "pdf2img",
    "watermark", "compress", "iconrender", "mediadownload",
    "watermark_remove",
}

_lock: asyncio.Lock | None = None
_rate_lock: asyncio.Lock | None = None

def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock

def _get_rate_lock() -> asyncio.Lock:
    global _rate_lock
    if _rate_lock is None:
        _rate_lock = asyncio.Lock()
    return _rate_lock

_heavy_call_log: dict[int, Deque[float]] = defaultdict(deque)
_rate_log_loaded = False


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _load_list(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


async def add_word(chat_id: int, word: str, replacement: str) -> tuple[bool, str]:
    word, replacement = word.strip().lower(), replacement.strip()
    if not word or not replacement:
        return False, "Both word and replacement must be non-empty."
    if word in RESERVED_WORDS:
        return False, f"'{word}' is reserved and cannot be overridden."
    async with _get_lock():
        data = _load(CUSTOM_WORDS_PATH)
        user_words = data.setdefault(str(chat_id), {})
        was_update = word in user_words
        if not was_update and len(user_words) >= MAX_WORDS_PER_USER:
            return False, f"Limit reached ({MAX_WORDS_PER_USER} words). Remove one with /delword first."
        user_words[word] = replacement
        _save(CUSTOM_WORDS_PATH, data)
    return True, "updated" if was_update else "added"


async def get_words(chat_id: int) -> dict[str, str]:
    async with _get_lock():
        return dict(_load(CUSTOM_WORDS_PATH).get(str(chat_id), {}))


async def del_word(chat_id: int, word: str) -> bool:
    word = word.strip().lower()
    async with _get_lock():
        data = _load(CUSTOM_WORDS_PATH)
        user_words = data.get(str(chat_id), {})
        if word not in user_words:
            return False
        del user_words[word]
        _save(CUSTOM_WORDS_PATH, data)
    return True


async def reset_words(chat_id: int) -> None:
    async with _get_lock():
        data = _load(CUSTOM_WORDS_PATH)
        data.pop(str(chat_id), None)
        _save(CUSTOM_WORDS_PATH, data)


async def record_usage(
    tool_name: str,
    chat_id: int | None = None,
    username: str | None = None,
) -> None:
    now = time.time()
    async with _get_lock():
        stats = _load(USAGE_STATS_PATH)
        stats[tool_name] = stats.get(tool_name, 0) + 1
        _save(USAGE_STATS_PATH, stats)
        if chat_id is not None:
            activity = _load(USER_ACTIVITY_PATH)
            entry = activity.setdefault(str(chat_id), {
                "username": username, "counts": {}, "last_seen": now, "log": [],
            })
            if username:
                entry["username"] = username
            entry["counts"][tool_name] = entry["counts"].get(tool_name, 0) + 1
            entry["last_seen"] = now
            entry["log"].append({"tool": tool_name, "ts": now})
            entry["log"] = entry["log"][-MAX_LOG_ENTRIES_PER_USER:]
            _save(USER_ACTIVITY_PATH, activity)


async def get_stats() -> list[tuple[str, int]]:
    async with _get_lock():
        data = _load(USAGE_STATS_PATH)
    return sorted(data.items(), key=lambda kv: kv[1], reverse=True)


async def get_user_activity_all() -> dict:
    async with _get_lock():
        return _load(USER_ACTIVITY_PATH)


async def get_user_activity(chat_id: int) -> dict | None:
    async with _get_lock():
        return _load(USER_ACTIVITY_PATH).get(str(chat_id))


async def find_chat_id_by_username(username: str) -> int | None:
    username = username.strip().lstrip("@").lower()
    async with _get_lock():
        data = _load(USER_ACTIVITY_PATH)
    for cid, entry in data.items():
        if (entry.get("username") or "").lower() == username:
            return int(cid)
    return None


async def set_watermark(chat_id: int, file_id: str) -> None:
    async with _get_lock():
        data = _load(WATERMARKS_PATH)
        data[str(chat_id)] = file_id
        _save(WATERMARKS_PATH, data)


async def get_watermark(chat_id: int) -> str | None:
    async with _get_lock():
        return _load(WATERMARKS_PATH).get(str(chat_id))


async def clear_watermark(chat_id: int) -> None:
    async with _get_lock():
        data = _load(WATERMARKS_PATH)
        data.pop(str(chat_id), None)
        _save(WATERMARKS_PATH, data)


def _ensure_rate_log_loaded() -> None:
    global _rate_log_loaded
    if _rate_log_loaded:
        return
    raw = _load(RATE_LIMIT_LOG_PATH)
    now = time.monotonic()
    real_now = time.time()
    for cid_str, timestamps in raw.items():
        dq = _heavy_call_log[int(cid_str)]
        for ts in timestamps:
            age = real_now - ts
            if age < RATE_LIMIT_WINDOW_SECONDS:
                dq.append(now - age)
    _rate_log_loaded = True


def _persist_rate_log() -> None:
    now_mono = time.monotonic()
    now_real = time.time()
    out: dict[str, list[float]] = {}
    for cid, dq in _heavy_call_log.items():
        out[str(cid)] = [now_real - (now_mono - ts) for ts in dq]
    _save(RATE_LIMIT_LOG_PATH, out)


async def set_custom_limit(chat_id: int, max_calls: int | None) -> None:
    async with _get_lock():
        data = _load(CUSTOM_LIMITS_PATH)
        if max_calls is None:
            data.pop(str(chat_id), None)
        else:
            data[str(chat_id)] = max(0, int(max_calls))
        _save(CUSTOM_LIMITS_PATH, data)


async def get_custom_limit(chat_id: int) -> int | None:
    async with _get_lock():
        return _load(CUSTOM_LIMITS_PATH).get(str(chat_id))


async def _effective_limit(chat_id: int) -> int:
    custom = await get_custom_limit(chat_id)
    return custom if custom is not None else RATE_LIMIT_HEAVY_MAX_CALLS


async def is_blocked(chat_id: int) -> bool:
    async with _get_lock():
        return chat_id in _load_list(BLOCKED_USERS_PATH)


async def block_user(chat_id: int) -> None:
    async with _get_lock():
        blocked = _load_list(BLOCKED_USERS_PATH)
        if chat_id not in blocked:
            blocked.append(chat_id)
            _save(BLOCKED_USERS_PATH, blocked)
    await set_custom_limit(chat_id, 0)


async def unblock_user(chat_id: int) -> None:
    async with _get_lock():
        blocked = _load_list(BLOCKED_USERS_PATH)
        if chat_id in blocked:
            blocked.remove(chat_id)
            _save(BLOCKED_USERS_PATH, blocked)
    await set_custom_limit(chat_id, None)


async def check_rate_limit(chat_id: int, exempt: bool = False) -> tuple[bool, float]:
    if exempt:
        return True, 0.0
    limit = await _effective_limit(chat_id)
    now = time.monotonic()
    async with _get_rate_lock():
        _ensure_rate_log_loaded()
        log = _heavy_call_log[chat_id]
        while log and now - log[0] > RATE_LIMIT_WINDOW_SECONDS:
            log.popleft()
        if limit <= 0:
            return False, float(RATE_LIMIT_WINDOW_SECONDS)
        if len(log) >= limit:
            wait = RATE_LIMIT_WINDOW_SECONDS - (now - log[0])
            return False, max(wait, 1.0)
        log.append(now)
        _persist_rate_log()
        return True, 0.0


async def calls_used(chat_id: int) -> tuple[int, int]:
    limit = await _effective_limit(chat_id)
    now = time.monotonic()
    async with _get_rate_lock():
        _ensure_rate_log_loaded()
        log = _heavy_call_log[chat_id]
        while log and now - log[0] > RATE_LIMIT_WINDOW_SECONDS:
            log.popleft()
        return len(log), limit


async def add_limit_request(chat_id: int, username: str | None, requested: int) -> None:
    async with _get_lock():
        data = _load(LIMIT_REQUESTS_PATH)
        data[str(chat_id)] = {"username": username, "requested": requested, "ts": time.time()}
        _save(LIMIT_REQUESTS_PATH, data)


async def get_limit_requests() -> dict:
    async with _get_lock():
        return _load(LIMIT_REQUESTS_PATH)


async def remove_limit_request(chat_id: int) -> None:
    async with _get_lock():
        data = _load(LIMIT_REQUESTS_PATH)
        data.pop(str(chat_id), None)
        _save(LIMIT_REQUESTS_PATH, data)
