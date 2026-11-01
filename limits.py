"""Daily limits per Telegram user, so nobody can run up the Claude bill.

Every conversation (chat or Telegram call), every message to the AI and every
minute of a call is counted per user per day in users.db (table "usage").
The numbers can be changed in config.py; ADMIN_IDS (your Telegram ids) have no limits.
"""
import os
import sqlite3
import threading
from datetime import date

import config

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")

CONVERSATIONS_PER_DAY = getattr(config, "LIMIT_CONVERSATIONS_PER_DAY", 5)
MESSAGES_PER_DAY = getattr(config, "LIMIT_MESSAGES_PER_DAY", 80)
MESSAGES_PER_CONVERSATION = getattr(config, "LIMIT_MESSAGES_PER_CONVERSATION", 40)
CALL_MINUTES_PER_DAY = getattr(config, "LIMIT_CALL_MINUTES_PER_DAY", 20)
ALL_USERS_MESSAGES_PER_DAY = getattr(config, "LIMIT_ALL_USERS_MESSAGES_PER_DAY", 3000)  # whole bot budget
ADMIN_IDS = set(getattr(config, "ADMIN_IDS", []))

_lock = threading.Lock()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage(
                user_id integer,
                day text,
                conversations integer default 0,
                messages integer default 0,
                call_seconds integer default 0,
                primary key (user_id, day)
            );
        """)


def _today(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT conversations, messages, call_seconds FROM usage WHERE user_id = ? AND day = ?",
            (user_id, date.today().isoformat()),
        ).fetchone()
    return row or (0, 0, 0)


def _add(user_id, column, amount=1):
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"INSERT INTO usage (user_id, day, {column}) VALUES (?, ?, ?) "
            f"ON CONFLICT(user_id, day) DO UPDATE SET {column} = {column} + excluded.{column}",
            (user_id, date.today().isoformat(), amount),
        )


def _all_users_messages():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COALESCE(SUM(messages), 0) FROM usage WHERE day = ?",
                            (date.today().isoformat(),)).fetchone()[0]


def start_conversation(user_id):
    """Counts a new conversation. Returns False if the user has used up today's conversations."""
    if user_id in ADMIN_IDS:
        return True
    if _today(user_id)[0] >= CONVERSATIONS_PER_DAY:
        return False
    _add(user_id, "conversations")
    return True


def add_message(user_id, messages_in_conversation):
    """Counts one message to the AI. Returns False if a limit is reached
    (per day, per conversation, or the whole bot's daily budget)."""
    if user_id in ADMIN_IDS:
        return True
    if (messages_in_conversation > MESSAGES_PER_CONVERSATION
            or _today(user_id)[1] >= MESSAGES_PER_DAY
            or _all_users_messages() >= ALL_USERS_MESSAGES_PER_DAY):
        return False
    _add(user_id, "messages")
    return True


def call_seconds_left(user_id):
    if user_id in ADMIN_IDS:
        return 24 * 3600
    return max(0, CALL_MINUTES_PER_DAY * 60 - _today(user_id)[2])


def add_call_seconds(user_id, seconds):
    if user_id not in ADMIN_IDS:
        _add(user_id, "call_seconds", int(seconds))
