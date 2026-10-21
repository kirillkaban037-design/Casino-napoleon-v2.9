# -*- coding: utf-8 -*-
"""
Napoleon Casino — Telegram Casino Bot (zarabatil.py)
Один файл. Python 3.10+.

Зависимости:  pip install "aiogram>=3.4" httpx python-dotenv

Запуск:  python zarabatil.py

Токены задаются ТОЛЬКО через переменные окружения / .env:
  BOT_TOKEN, CRYPTOBOT_TOKEN (можно также сменить CryptoBot-токен в админке), OWNER_IDS
Всё (названия, лимиты, комиссии, RTP, коэффициенты, токены, рефералка, бонусы,
каналы, техработы и т.д.) меняется из бота: кнопка «👑 Админ».
"""

import asyncio
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import platform
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, InvalidOperation

import httpx

import aiogram
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import DiceEmoji, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ===================== CONFIG =====================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8938155673:AAFjLwOeKMdiksdFlAHd1-yGjm_kblRoImU").strip()
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "655781:AAiSXDDOSYqHx5xSVsyHRin5f6ARhTX4nCJ").strip()

OWNER_IDS = {8221599718}
OWNER_IDS |= {int(x) for x in re.split(r"[,\s]+", os.getenv("OWNER_IDS", "")) if x.isdigit()}

DB_PATH = os.getenv("DATABASE_PATH", "napoleon.sqlite3").strip()
PORT = int(os.getenv("PORT", "10000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("napoleon")

CRYPTO_API = os.getenv("CRYPTO_API", "https://pay.crypt.bot/api").strip().rstrip("/")

SEP = "━━━━━━━━━━━━━━━━━━━━"
STARTED_AT = time.time()

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан. Укажите его в переменной окружения или в файле .env")


class GameError(Exception):
    """Понятная пользователю ошибка / определённый отказ."""


class ProviderUnknown(Exception):
    """Статус операции у платёжного провайдера неизвестен (сеть, таймаут)."""


# ===================== UTILS =====================

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def since(days: float = 0, minutes: float = 0, seconds: float = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days, minutes=minutes, seconds=seconds)
    return dt.isoformat(timespec="seconds")


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def money(v) -> str:
    return f"{Decimal(int(v)) / 100:,.2f}".replace(",", " ") + " USDT"


def usdt(v) -> str:
    return f"{Decimal(int(v)) / 100:.2f}"


def parse_money(t, allow_zero=False):
    try:
        d = Decimal(str(t).strip().replace(",", ".").replace("$", ""))
    except (InvalidOperation, AttributeError):
        return None
    if not d.is_finite() or d < 0:
        return None
    cents = int((d * 100).to_integral_value(ROUND_DOWN))
    if cents == 0 and not allow_zero:
        return None
    return cents


def dstr(d) -> str:
    """Decimal -> строка без экспоненты (10 -> '10', а не '1E+1')."""
    return format(Decimal(d).normalize(), "f")


def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def mask(v: str) -> str:
    if not v:
        return "—"
    return v[:6] + "…" + v[-4:] if len(v) > 14 else "***"


# ===================== DATABASE =====================

@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
    finally:
        c.close()


@contextmanager
def tx():
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def q1(sql, a=()):
    with db() as c:
        r = c.execute(sql, a).fetchone()
        return dict(r) if r else None


def qa(sql, a=()):
    with db() as c:
        return [dict(x) for x in c.execute(sql, a)]


def ex(sql, a=()):
    with db() as c:
        c.execute(sql, a)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT NOT NULL,
    referrer_id INTEGER,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    vip_level TEXT NOT NULL DEFAULT 'Новичок',
    wd_blocked INTEGER NOT NULL DEFAULT 0,
    max_stake INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    last_daily TEXT
);
CREATE TABLE IF NOT EXISTS balances(
    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    balance INTEGER NOT NULL CHECK(balance >= 0),
    ref_balance INTEGER NOT NULL DEFAULT 0,
    total_wagered INTEGER NOT NULL DEFAULT 0,
    reserved_balance INTEGER NOT NULL DEFAULT 0,
    bonus_need INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    balance_before INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason TEXT NOT NULL,
    actor_id INTEGER,
    round_id TEXT,
    idempotency_key TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(telegram_id, id);
CREATE TABLE IF NOT EXISTS rounds(
    round_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    game TEXT NOT NULL,
    state TEXT NOT NULL,
    stake INTEGER NOT NULL,
    coeff TEXT NOT NULL,
    rules_version INTEGER NOT NULL DEFAULT 1,
    data_json TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    payout INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    server_seed TEXT,
    client_seed TEXT,
    server_seed_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_round_user ON rounds(user_id, started_at);
CREATE INDEX IF NOT EXISTS idx_round_state ON rounds(state, started_at);
CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS game_settings(
    game TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    coeff TEXT NOT NULL DEFAULT '1.50',
    title TEXT,
    min_stake INTEGER NOT NULL DEFAULT 0,
    max_stake INTEGER NOT NULL DEFAULT 0,
    win_min INTEGER NOT NULL DEFAULT 4,
    rtp TEXT NOT NULL DEFAULT '95',
    coeff_override TEXT NOT NULL DEFAULT '0'
);
CREATE TABLE IF NOT EXISTS outcome_settings(
    game TEXT NOT NULL,
    key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    coeff_override TEXT NOT NULL DEFAULT '0',
    PRIMARY KEY(game, key)
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices(
    invoice_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    paid_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_inv_status ON invoices(status, created_at);
CREATE TABLE IF NOT EXISTS withdrawals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    address TEXT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    tx_id TEXT,
    admin_note TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS user_seeds(
    user_id INTEGER PRIMARY KEY,
    client_seed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admins(
    telegram_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promos(
    code TEXT PRIMARY KEY,
    amount INTEGER NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 1,
    used INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_uses(
    code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(code, user_id)
);
"""

MIGRATIONS = [
    ("users", "wd_blocked", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "max_stake", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "note", "TEXT NOT NULL DEFAULT ''"),
    ("users", "last_daily", "TEXT"),
    ("balances", "bonus_need", "INTEGER NOT NULL DEFAULT 0"),
    ("balances", "reserved_balance", "INTEGER NOT NULL DEFAULT 0"),
    ("game_settings", "rtp", "TEXT NOT NULL DEFAULT '95'"),
    ("game_settings", "coeff_override", "TEXT NOT NULL DEFAULT '0'"),
    ("game_settings", "win_min", "INTEGER NOT NULL DEFAULT 4"),
    ("rounds", "server_seed", "TEXT"),
    ("rounds", "client_seed", "TEXT"),
    ("rounds", "server_seed_hash", "TEXT"),
]

SCHEMA_VERSION = "8"


def migrate(c):
    for table, col, ddl in MIGRATIONS:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            log.info("migration: %s.%s added", table, col)


# ===================== SETTINGS REGISTRY =====================
# key: (group, label, kind, default, hint)
# kinds: str, text, usdt, pct, num, int, bool, list, secret, chan, choice:a,b

GROUPS = {
    "general": "🏛 Основное",
    "finance": "💳 Финансы",
    "games": "🎮 Игры",
    "bonus": "🎁 Бонусы",
    "ref": "👥 Рефералы",
    "channel": "📣 Канал побед",
    "security": "🛡 Безопасность",
    "tokens": "🔑 Токены API",
}

SETTINGS = {
    # general
    "casino_name": ("general", "Название казино", "str", "Napoleon Casino", "Показывается в главном меню"),
    "support_username": ("general", "Поддержка (юзернейм)", "str", "@support", "Например @mysupport"),
    "news_url": ("general", "Ссылка на канал/новости", "str", "", "https://t.me/... или «-» чтобы убрать"),
    "welcome_text": ("general", "Приветствие", "text", "🔥 Добро пожаловать в элитный клуб!", "Текст под балансом в меню (HTML)"),
    "rules_text": ("general", "Правила", "text", "Играйте ответственно. Минимальный возраст — 18 лет. Администрация вправе блокировать аккаунты за мошенничество.", "Текст раздела «Правила»"),
    "maintenance": ("general", "Техработы (вкл/выкл)", "bool", "0", "Пока включено — игроки не могут пользоваться ботом"),
    "maintenance_text": ("general", "Сообщение о техработах", "text", "🛠 Идут технические работы. Скоро вернёмся!", ""),
    # finance
    "min_deposit": ("finance", "Мин. депозит", "usdt", "10", "USDT"),
    "max_deposit": ("finance", "Макс. депозит", "usdt", "100000", "USDT"),
    "min_withdraw": ("finance", "Мин. вывод", "usdt", "10", "USDT"),
    "max_withdraw": ("finance", "Макс. вывод", "usdt", "100000", "USDT"),
    "daily_deposit_limit": ("finance", "Лимит депозитов в сутки", "usdt", "50000", "На одного игрока"),
    "daily_withdraw_limit": ("finance", "Лимит выводов в сутки", "usdt", "100000", "На одного игрока"),
    "wagering": ("finance", "Вейджер (x от депозитов)", "num", "1.5", "Сколько оборота нужно для вывода"),
    "fee_cryptobot": ("finance", "Комиссия депозита CryptoBot", "pct", "2.9", "% удерживается с депозита"),
    "fee_win": ("finance", "Комиссия с выигрыша", "pct", "0", "% от чистой прибыли раунда. Показывается игрокам в играх; снижает фактический RTP"),
    "dep_cb_on": ("finance", "Депозит через CryptoBot", "bool", "1", ""),
    "wd_cb_on": ("finance", "Вывод через CryptoBot", "bool", "1", ""),
    "auto_withdraw": ("finance", "Авто-вывод", "bool", "1", "Выкл — все заявки идут на ручную проверку"),
    "auto_wd_max": ("finance", "Авто-вывод до суммы", "usdt", "500", "Заявки больше — на ручную проверку админом"),
    "wd_cooldown_min": ("finance", "Пауза между выводами (мин)", "int", "60", ""),
    "invoice_ttl_min": ("finance", "Время жизни счёта (мин)", "int", "30", "Минимум 5"),
    "max_pending_invoices": ("finance", "Макс. неоплаченных счетов", "int", "3", ""),
    "dep_quick": ("finance", "Быстрые суммы депозита", "list", "10,50,100,250,500", "Через запятую, USDT"),
    # games
    "min_stake": ("games", "Мин. ставка (общая)", "usdt", "1", "Можно переопределить в игре"),
    "max_stake": ("games", "Макс. ставка (общая)", "usdt", "1000", "Можно переопределить в игре"),
    "max_win": ("games", "Макс. выплата за раунд", "usdt", "5000", "0 — без ограничения"),
    "round_timeout": ("games", "Таймаут раунда (мин)", "int", "10", "Мины/Башня: авто-кэшаут или возврат"),
    "stake_quick": ("games", "Быстрые ставки", "list", "1,5,10,50,100", "Через запятую, USDT"),
    "mines_counts": ("games", "Варианты кол-ва мин", "list", "1,3,5,10", "Числа от 1 до 24"),
    "tower_floors": ("games", "Варианты высоты башни", "list", "5,8,10", "Числа от 2 до 30"),
    "show_rtp": ("games", "Показывать RTP игрокам", "bool", "1", "RTP и коэффициенты видны в меню игр"),
    # bonus
    "daily_bonus_on": ("bonus", "Ежедневный бонус", "bool", "1", ""),
    "daily_bonus_amount": ("bonus", "Сумма ежедневного бонуса", "usdt", "0.50", ""),
    "daily_bonus_cd_h": ("bonus", "Интервал бонуса (часы)", "int", "24", ""),
    "daily_bonus_min_dep": ("bonus", "Мин. сумма депозитов для бонуса", "usdt", "10", "0 — без условия"),
    "bonus_wager": ("bonus", "Вейджер бонусов (x)", "num", "10", "Оборот, нужный для вывода после бонуса/промо"),
    "promo_on": ("bonus", "Промокоды", "bool", "1", ""),
    # referral
    "ref_enabled": ("ref", "Реферальная программа", "bool", "1", ""),
    "ref_percent": ("ref", "Реферальный процент", "pct", "1", ""),
    "ref_basis": ("ref", "База начисления", "choice:win,stake", "win", "win — с прибыли друга, stake — со ставок друга"),
    # channel
    "wins_channel_id": ("channel", "ID/юзернейм канала побед", "chan", "", "@channel или -100…; «-» чтобы отключить"),
    "win_threshold": ("channel", "Порог публикации выигрыша", "usdt", "500", ""),
    "wins_notify": ("channel", "Публиковать победы", "bool", "1", ""),
    # security
    "throttle_ms": ("security", "Антиспам (мс между действиями)", "int", "400", "0 — выключить"),
    # tokens
    "cryptobot_token": ("tokens", "CryptoBot API токен", "secret", "", "Пусто — токен из переменной окружения CRYPTOBOT_TOKEN"),
}

GROUP_KEYS = {g: [k for k, v in SETTINGS.items() if v[0] == g] for g in GROUPS}

_SC: dict = {}


def S(k):
    if k in _SC:
        return _SC[k]
    r = q1("SELECT value FROM settings WHERE key=?", (k,))
    v = r["value"] if r else SETTINGS.get(k, ("", "", "", ""))[3]
    _SC[k] = v
    return v


def Sd(k):
    try:
        return Decimal(S(k))
    except InvalidOperation:
        return Decimal(SETTINGS[k][3] or "0")


def Si(k):
    """Настройка в USDT -> центы."""
    return int((Sd(k) * 100).to_integral_value(ROUND_DOWN))


def Sn(k):
    try:
        return int(Decimal(S(k)))
    except (InvalidOperation, ValueError):
        return int(Decimal(SETTINGS[k][3] or "0"))


def Sb(k):
    return S(k) == "1"


def Lc(k):
    out = []
    for x in S(k).split(","):
        try:
            out.append(int((Decimal(x.strip()) * 100).to_integral_value(ROUND_DOWN)))
        except (InvalidOperation, ValueError):
            pass
    return [x for x in out if x > 0]


def Li(k):
    out = []
    for x in S(k).split(","):
        try:
            out.append(int(Decimal(x.strip())))
        except (InvalidOperation, ValueError):
            pass
    return out


def set_setting(k, v):
    ex(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (k, str(v)),
    )
    _SC[k] = str(v)


def cb_token():
    return S("cryptobot_token") or CRYPTOBOT_TOKEN


def validate_setting(kind, raw):
    raw = raw.strip()
    if kind in ("str", "text", "secret", "chan"):
        if raw == "-":
            return ""
        if len(raw) > (3500 if kind == "text" else 300):
            raise ValueError("Слишком длинное значение")
        if kind == "chan" and raw and not re.fullmatch(r"(@[A-Za-z0-9_]{4,32}|-?\d{5,20})", raw):
            raise ValueError("Нужен @username или числовой ID (-100…)")
        return raw
    if kind in ("usdt", "pct", "num"):
        d = Decimal(raw.replace(",", ".").replace("$", "").replace("%", ""))
        if not d.is_finite() or d < 0:
            raise ValueError("Нужно неотрицательное число")
        if kind == "pct" and d > 100:
            raise ValueError("Процент не больше 100")
        if d > Decimal("1000000000"):
            raise ValueError("Слишком большое число")
        return dstr(d.quantize(Decimal("0.01"), ROUND_DOWN)) if kind != "num" else dstr(d)
    if kind == "int":
        n = int(raw)
        if n < 0 or n > 10_000_000:
            raise ValueError("Нужно целое число от 0")
        return str(n)
    if kind == "list":
        parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        if not parts or len(parts) > 12:
            raise ValueError("От 1 до 12 чисел через запятую")
        vals = []
        for p in parts:
            d = Decimal(p.replace(",", "."))
            if d <= 0:
                raise ValueError("Числа должны быть больше нуля")
            vals.append(dstr(d))
        return ",".join(vals)
    if kind.startswith("choice:"):
        opts = kind.split(":", 1)[1].split(",")
        if raw not in opts:
            raise ValueError("Допустимо: " + ", ".join(opts))
        return raw
    raise ValueError("Неизвестный тип")


def show_val(k):
    kind = SETTINGS[k][2]
    v = S(k)
    if kind == "bool":
        return "✅ вкл" if v == "1" else "⛔ выкл"
    if kind == "secret":
        return mask(v) if v else "из кода"
    if kind == "usdt":
        return f"{v} USDT"
    if kind == "pct":
        return f"{v}%"
    if not v:
        return "—"
    v = v.replace("\n", " ")
    return v if len(v) <= 28 else v[:27] + "…"


# ===================== GAME SETTINGS =====================

GAMES = {
    "mines": ("💣", "Мины", "Открывайте безопасные клетки 5×5, наращивайте множитель и забирайте выигрыш в любой момент."),
    "tower": ("🏙", "Башня", "Поднимайтесь по этажам. Одна ловушка на этаже — чем выше, тем больше множитель."),
    "coin": ("🪙", "Монетка", "Орёл или решка. Честный провайдер seed, мгновенный результат."),
    "slots": ("🎰", "Слоты", "Крутите барабаны Telegram: тройные комбинации дают крупные множители."),
    "football": ("⚽", "Футбол", "Один точный удар — и победа ваша!"),
    "basketball": ("🏀", "Баскетбол", "Бросок в кольцо — почувствуйте вкус триумфа."),
    "darts": ("🎯", "Дартс", "Попадите в цель и заберите награду."),
    "dice": ("🎲", "Кубик", "Классика на чистую удачу."),
}

# game: (rtp %, win_min [устарело], title)
GAME_DEFAULTS = {
    "mines": ("97", 4, "Мины"),
    "tower": ("97", 4, "Башня"),
    "coin": ("95", 4, "Монетка"),
    "slots": ("90", 4, "Слоты"),
    "football": ("95", 3, "Футбол"),
    "basketball": ("95", 4, "Баскетбол"),
    "darts": ("95", 4, "Дартс"),
    "dice": ("95", 4, "Кубик"),
}

DICE = {
    "football": DiceEmoji.FOOTBALL,
    "basketball": DiceEmoji.BASKETBALL,
    "darts": DiceEmoji.DART,
    "dice": DiceEmoji.DICE,
    "slots": DiceEmoji.SLOT_MACHINE,
}
DICE_WAIT = {"football": 4.0, "basketball": 4.0, "darts": 3.5, "dice": 3.5, "slots": 2.5}

# Игры на анимированных эмодзи Telegram:
# game -> (число значений, {исход: (подпись, выигрышные значения)})
OUTCOMES = {
    "dice": (6, {
        "over": ("⬆️ Больше (4–6)", (4, 5, 6)),
        "under": ("⬇️ Меньше (1–3)", (1, 2, 3)),
        "even": ("2️⃣ Чёт (2, 4, 6)", (2, 4, 6)),
        "odd": ("1️⃣ Нечет (1, 3, 5)", (1, 3, 5)),
        "s1": ("1️⃣ сектор (1–2)", (1, 2)),
        "s2": ("2️⃣ сектор (3–4)", (3, 4)),
        "s3": ("3️⃣ сектор (5–6)", (5, 6)),
    }),
    "football": (5, {
        "goal": ("⚽ Гол", (3, 4, 5)),
        "post": ("🥅 Штанга / перекладина", (2,)),
        "miss": ("❌ Мимо", (1,)),
    }),
    "basketball": (5, {
        "hoop": ("🏀 В кольцо", (4, 5)),
        "stuck": ("😬 Застрял", (3,)),
        "miss": ("❌ Мимо", (1, 2)),
    }),
    "darts": (6, {
        "center": ("🎯 Середина / яблочко", (6,)),
        "red": ("🔴 Красное", (2, 4)),
        "white": ("⚪ Белое", (3, 5)),
        "miss": ("❌ Мимо", (1,)),
    }),
}
DICE_MAX = {g: v[0] for g, v in OUTCOMES.items()}

# Слоты (64 значения, каждая тройка выпадает с шансом 1/64). Соотношение выплат
# фиксировано, абсолютные множители масштабируются под RTP игры.
SLOT_VAL = {64: "777", 1: "bar", 22: "grape", 43: "lemon"}
SLOT_RATIO = {"777": 25, "bar": 12, "grape": 10, "lemon": 10}
SLOT_LABEL = {"777": "7️⃣7️⃣7️⃣", "bar": "BAR BAR BAR", "grape": "🍇🍇🍇", "lemon": "🍋🍋🍋"}
COIN_LABEL = {"h": "🦅 Орёл", "t": "🪙 Решка"}

TOWER_CELLS = {"easy": 4, "std": 3, "hard": 2}
TOWER_NAME = {"easy": "Лёгкий (1 из 4)", "std": "Стандарт (1 из 3)", "hard": "Сложный (1 из 2)"}


def gs(game):
    return q1("SELECT * FROM game_settings WHERE game=?", (game,)) or {
        "game": game, "enabled": 0, "coeff": "1.50", "title": None,
        "min_stake": 0, "max_stake": 0, "win_min": 4, "rtp": "95", "coeff_override": "0",
    }


def gtitle(game):
    emoji, default, _ = GAMES[game]
    t = (gs(game).get("title") or default).strip() or default
    return f"{emoji} {t}"


def game_rtp(game) -> Decimal:
    """RTP игры как доля (0.95), ограничен 1%…100%."""
    try:
        v = Decimal(gs(game)["rtp"]) / 100
    except InvalidOperation:
        v = Decimal("0.95")
    return min(max(v, Decimal("0.01")), Decimal(1))


# ---------- исходы и коэффициенты ----------

def outcome_keys(game):
    if game == "coin":
        return ["h", "t"]
    if game == "slots":
        return list(SLOT_RATIO)
    return list(OUTCOMES[game][1])


def outcome_label(game, key):
    if game == "coin":
        return COIN_LABEL.get(key, key)
    if game == "slots":
        return SLOT_LABEL.get(key, key)
    return OUTCOMES[game][1][key][0]


def outcome_values(game, key):
    if game in OUTCOMES:
        return OUTCOMES[game][1][key][1]
    return ()


def outcome_row(game, key):
    return q1("SELECT * FROM outcome_settings WHERE game=? AND key=?", (game, key)) or {
        "game": game, "key": key, "enabled": 1, "coeff_override": "0",
    }


def outcome_enabled(game, key) -> bool:
    if game == "slots":
        return True
    return bool(outcome_row(game, key)["enabled"])


def enabled_outcomes(game):
    return [k for k in outcome_keys(game) if outcome_enabled(game, k)]


def outcome_override(game, key) -> Decimal:
    try:
        v = Decimal(outcome_row(game, key).get("coeff_override") or "0")
    except InvalidOperation:
        v = Decimal(0)
    return v if v > 0 else Decimal(0)


def outcome_prob(game, key) -> Decimal:
    if game == "coin":
        return Decimal(1) / 2
    if game == "slots":
        return Decimal(1) / 64
    n, items = OUTCOMES[game]
    return Decimal(len(items[key][1])) / n


def outcome_auto_coeff(game, key) -> Decimal:
    rtp = game_rtp(game)
    if game == "slots":
        total = sum(SLOT_RATIO.values())
        c = Decimal(SLOT_RATIO[key]) * rtp * 64 / total
    else:
        c = rtp / outcome_prob(game, key)
    return max(c.quantize(Decimal("0.01"), ROUND_DOWN), Decimal("1.00"))


def outcome_coeff(game, key) -> Decimal:
    ov = outcome_override(game, key)
    return ov if ov > 0 else outcome_auto_coeff(game, key)


def outcome_rtp(game, key) -> Decimal:
    """Фактический RTP исхода (доля): коэффициент × вероятность."""
    return outcome_coeff(game, key) * outcome_prob(game, key)


def slots_rtp() -> Decimal:
    return sum(outcome_coeff("slots", k) for k in SLOT_RATIO) / 64


def displayed_rtp(game) -> str:
    """Честный RTP по реальным коэффициентам (учитывает ручные коэффициенты)."""
    if game in ("mines", "tower"):
        return f"{game_rtp(game) * 100:.1f}%"
    if game == "slots":
        return f"{slots_rtp() * 100:.1f}%"
    vals = [outcome_rtp(game, k) * 100 for k in enabled_outcomes(game)] or [game_rtp(game) * 100]
    lo, hi = min(vals), max(vals)
    if hi - lo < Decimal("0.05"):
        return f"{lo:.1f}%"
    return f"{lo:.1f}–{hi:.1f}%"


def rtp_tag(game) -> str:
    return f" · RTP {displayed_rtp(game)}" if Sb("show_rtp") else ""


def fee_line() -> str:
    f = Sd("fee_win")
    return f"\n💸 Комиссия с чистой прибыли раунда: <b>{dstr(f)}%</b>" if f > 0 else ""


def cap_line() -> str:
    cap = Si("max_win")
    return f"\n🔝 Макс. выплата за раунд: <b>{money(cap)}</b>" if cap > 0 else ""


def set_outcome(game, key, enabled=None, coeff=None):
    ex("INSERT OR IGNORE INTO outcome_settings(game, key) VALUES(?,?)", (game, key))
    if enabled is not None:
        ex("UPDATE outcome_settings SET enabled=? WHERE game=? AND key=?", (1 if enabled else 0, game, key))
    if coeff is not None:
        ex("UPDATE outcome_settings SET coeff_override=? WHERE game=? AND key=?", (str(coeff), game, key))


def stake_bounds(game, uid=None):
    g = gs(game)
    lo = g["min_stake"] or Si("min_stake")
    hi = g["max_stake"] or Si("max_stake")
    if uid:
        u = q1("SELECT max_stake FROM users WHERE telegram_id=?", (uid,))
        if u and u["max_stake"] > 0:
            hi = min(hi, u["max_stake"])
    return lo, hi


def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        migrate(c)
        for k, v in SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v[3]))
        for g, (rtp, wmin, title) in GAME_DEFAULTS.items():
            c.execute(
                "INSERT OR IGNORE INTO game_settings(game, enabled, coeff, title, win_min, rtp) VALUES(?,?,?,?,?,?)",
                (g, 1, "1.50", title, wmin, rtp),
            )
        c.execute(
            "INSERT INTO settings(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        # незавершённые выплаты после падения — на ручную сверку
        n = c.execute("UPDATE withdrawals SET status='UNKNOWN', admin_note='Прервано перезапуском' WHERE status='PROCESSING'").rowcount
        if n:
            log.warning("%s выплат в статусе PROCESSING переведены в UNKNOWN", n)
        # xRocket удалён: неоплаченные счета закрываем, ожидающие выплаты — на ручную проверку
        c.execute("UPDATE invoices SET status='EXPIRED' WHERE invoice_id LIKE 'xr:%' AND status='PENDING'")
        c.execute(
            "UPDATE withdrawals SET status='REVIEW', admin_note='xRocket отключён: выплатите вручную или отклоните' "
            "WHERE provider='xrocket' AND status='PENDING'"
        )
    _SC.clear()
    load_admins()


ADMINS: set = set()


def load_admins():
    ADMINS.clear()
    ADMINS.update(r["telegram_id"] for r in qa("SELECT telegram_id FROM admins"))


def is_owner(uid):
    return uid in OWNER_IDS


def is_admin(uid):
    return uid in OWNER_IDS or uid in ADMINS


def audit(actor, action, details):
    ex(
        "INSERT INTO audit(actor_id, action, details, created_at) VALUES(?,?,?,?)",
        (actor, action, str(details)[:1000], now()),
    )


# ===================== VIP / USERS =====================

VIP_LEVELS = [
    (100000, "Элита ♦"),
    (20000, "VIP"),
    (5000, "Хайроллер"),
    (1000, "Игрок"),
    (0, "Новичок"),
]


def vip_status(wagered_cents: int) -> str:
    for th, name in VIP_LEVELS:
        if wagered_cents >= th * 100:
            return name
    return "Новичок"


def ensure_user(uid, username, name, ref=None):
    with tx() as c:
        r = c.execute("SELECT 1 FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not r:
            if ref and (ref == uid or not c.execute("SELECT 1 FROM users WHERE telegram_id=?", (ref,)).fetchone()):
                ref = None
            c.execute(
                "INSERT INTO users(telegram_id, username, name, referrer_id, created_at, last_seen) VALUES(?,?,?,?,?,?)",
                (uid, username, name, ref, now(), now()),
            )
            c.execute(
                "INSERT INTO balances(telegram_id, balance, updated_at) VALUES(?,0,?)",
                (uid, now()),
            )
            return True
        c.execute(
            "UPDATE users SET username=?, name=?, last_seen=? WHERE telegram_id=?",
            (username, name, now(), uid),
        )
        return False


def get_user(uid):
    return q1(
        """SELECT u.*, b.balance, b.ref_balance, b.total_wagered, b.reserved_balance, b.bonus_need
           FROM users u JOIN balances b USING(telegram_id) WHERE u.telegram_id=?""",
        (uid,),
    )


def avail_of(u):
    return u["balance"] - u["reserved_balance"]


def user_status(uid):
    r = q1("SELECT status FROM users WHERE telegram_id=?", (uid,))
    return r["status"] if r else None


def count_rounds(uid):
    return q1("SELECT COUNT(*) n FROM rounds WHERE user_id=?", (uid,))["n"]


def add_ledger(c, uid, delta, before, after, reason, actor=None, rid=None, key=None):
    c.execute(
        """INSERT INTO ledger(telegram_id, delta, balance_before, balance_after,
                              reason, actor_id, round_id, idempotency_key, created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (uid, delta, before, after, reason, actor, rid, key or f"x:{secrets.token_hex(10)}", now()),
    )


def change_balance(target, delta, reason, actor, set_to=None):
    with tx() as c:
        r = c.execute(
            "SELECT balance, reserved_balance FROM balances WHERE telegram_id=?", (target,)
        ).fetchone()
        if not r:
            raise GameError("Пользователь не найден")
        before = r["balance"]
        after = set_to if set_to is not None else before + delta
        delta = after - before
        if after < 0:
            raise GameError("Баланс не может быть отрицательным")
        if after < r["reserved_balance"]:
            raise GameError("Баланс не может быть ниже зарезервированной суммы")
        c.execute("UPDATE balances SET balance=?, updated_at=? WHERE telegram_id=?", (after, now(), target))
        add_ledger(c, target, delta, before, after, f"admin: {reason}", actor)
        c.execute(
            "INSERT INTO audit(actor_id, action, details, created_at) VALUES(?,?,?,?)",
            (actor, "balance", f"{target} {delta:+d} {reason}"[:1000], now()),
        )
    return before, after


def get_client_seed(uid):
    r = q1("SELECT client_seed FROM user_seeds WHERE user_id=?", (uid,))
    if r:
        return r["client_seed"]
    seed = secrets.token_hex(8)
    ex("INSERT OR IGNORE INTO user_seeds(user_id, client_seed) VALUES(?,?)", (uid, seed))
    return get_client_seed(uid) if not q1("SELECT 1 x FROM user_seeds WHERE user_id=? AND client_seed=?", (uid, seed)) else seed


# ===================== PROVABLY FAIR =====================

def _stream(server_seed, client_seed, nonce):
    counter = 0
    while True:
        d = hmac.new(
            server_seed.encode(), f"{client_seed}:{nonce}:{counter}".encode(), hashlib.sha256
        ).digest()
        for i in range(0, 32, 4):
            yield int.from_bytes(d[i:i + 4], "big")
        counter += 1


def pf_shuffle(n, server_seed, client_seed, nonce):
    items = list(range(n))
    s = _stream(server_seed, client_seed, nonce)
    for i in range(n - 1, 0, -1):
        j = next(s) % (i + 1)
        items[i], items[j] = items[j], items[i]
    return items


def pf_traps(floors, cells, server_seed, client_seed, nonce):
    s = _stream(server_seed, client_seed, nonce)
    return [next(s) % cells for _ in range(floors)]


def seed_hash(server_seed):
    return hashlib.sha256(server_seed.encode()).hexdigest()


# ===================== ROUNDS =====================

win_queue: asyncio.Queue = asyncio.Queue()
round_locks = defaultdict(asyncio.Lock)
busy_users = set()


def start_round(rid, uid, game, stake, coeff, data, server_seed=None, client_seed=None):
    with tx() as c:
        b = c.execute(
            "SELECT balance, reserved_balance, total_wagered FROM balances WHERE telegram_id=?", (uid,)
        ).fetchone()
        if not b or b["balance"] - b["reserved_balance"] < stake:
            raise GameError("Недостаточно доступных средств")
        if c.execute("SELECT 1 FROM rounds WHERE user_id=? AND state='ACTIVE'", (uid,)).fetchone():
            raise GameError("active")
        before = b["balance"]
        after = before - stake
        wag = b["total_wagered"] + stake
        c.execute(
            "UPDATE balances SET balance=?, total_wagered=?, updated_at=? WHERE telegram_id=?",
            (after, wag, now(), uid),
        )
        c.execute("UPDATE users SET vip_level=? WHERE telegram_id=?", (vip_status(wag), uid))
        add_ledger(c, uid, -stake, before, after, f"ставка {game}", None, rid, f"stake:{rid}")
        c.execute(
            """INSERT INTO rounds(round_id, user_id, game, state, stake, coeff,
                   data_json, started_at, server_seed, client_seed, server_seed_hash)
               VALUES(?,?,?,'ACTIVE',?,?,?,?,?,?,?)""",
            (rid, uid, game, stake, str(coeff), json.dumps(data, separators=(",", ":")), now(),
             server_seed, client_seed, seed_hash(server_seed) if server_seed else None),
        )


def get_round(rid):
    return q1("SELECT * FROM rounds WHERE round_id=?", (rid,))


def active_round(uid):
    return q1(
        "SELECT * FROM rounds WHERE user_id=? AND state='ACTIVE' ORDER BY started_at DESC LIMIT 1", (uid,)
    )


def set_round_data(rid, data):
    ex(
        "UPDATE rounds SET data_json=? WHERE round_id=? AND state='ACTIVE'",
        (json.dumps(data, separators=(",", ":")), rid),
    )


def finish_round(rid, state, payout, result, fee=True):
    """Закрывает раунд. Возвращает чистую выплату или None, если раунд уже закрыт."""
    notify = None
    cap = Si("max_win")
    with tx() as c:
        r = c.execute("SELECT * FROM rounds WHERE round_id=?", (rid,)).fetchone()
        if not r or r["state"] != "ACTIVE":
            return None
        stake = r["stake"]
        gross = payout
        if cap > 0 and gross > cap and state != "REFUNDED":
            gross = cap
        net = gross
        if fee and state != "REFUNDED" and gross > stake:
            net = gross - int(Decimal(gross - stake) * Sd("fee_win") / 100)
        c.execute(
            "UPDATE rounds SET state=?, payout=?, result=?, ended_at=? WHERE round_id=?",
            (state, net, result, now(), rid),
        )
        uid = r["user_id"]
        if state == "REFUNDED":
            c.execute(
                "UPDATE balances SET total_wagered=MAX(0,total_wagered-?) WHERE telegram_id=?", (stake, uid)
            )
        if net > 0:
            b = c.execute("SELECT balance FROM balances WHERE telegram_id=?", (uid,)).fetchone()
            c.execute(
                "UPDATE balances SET balance=balance+?, updated_at=? WHERE telegram_id=?", (net, now(), uid)
            )
            reason = "возврат ставки" if state == "REFUNDED" else "выплата"
            add_ledger(c, uid, net, b["balance"], b["balance"] + net, f"{reason} {r['game']}", None, rid, f"payout:{rid}")

        u = c.execute("SELECT referrer_id, name FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if state != "REFUNDED" and u and u["referrer_id"] and S("ref_enabled") == "1":
            base = stake if S("ref_basis") == "stake" else max(0, net - stake)
            bonus = int(Decimal(base) * Sd("ref_percent") / 100)
            rb = c.execute("SELECT balance FROM balances WHERE telegram_id=?", (u["referrer_id"],)).fetchone()
            if bonus > 0 and rb:
                c.execute(
                    "UPDATE balances SET balance=balance+?, ref_balance=ref_balance+?, updated_at=? WHERE telegram_id=?",
                    (bonus, bonus, now(), u["referrer_id"]),
                )
                add_ledger(c, u["referrer_id"], bonus, rb["balance"], rb["balance"] + bonus,
                           f"реферальный бонус от {uid}", None, rid, f"ref:{rid}")

        if net >= Si("win_threshold") and net > stake and S("wins_channel_id") and Sb("wins_notify"):
            notify = (u["name"] if u else str(uid), r["game"], net)

    round_locks.pop(rid, None)
    if notify:
        win_queue.put_nowait(notify)
    return net


def refund_round(rid, why):
    r = get_round(rid)
    if not r:
        return None
    return finish_round(rid, "REFUNDED", r["stake"], why, fee=False)


# ===================== PAYMENTS =====================

_client = None


def http_client():
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    return _client


async def http_json(method, url, retries=0, **kw):
    last = None
    for _ in range(retries + 1):
        try:
            r = await http_client().request(method, url, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
            await asyncio.sleep(1)
            continue
        try:
            return r.json()
        except ValueError:
            raise ProviderUnknown(f"HTTP {r.status_code}")
    raise ProviderUnknown(f"network: {last}")


def cb_headers():
    return {"Crypto-Pay-API-Token": cb_token()}


def cb_err(data):
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("name") or err.get("message") or err.get("code") or "CryptoBot error")
    return str(err or "CryptoBot error")


def cb_error_kind(msg: str) -> str:
    """Классификация ошибки CryptoBot при выплате:
    dup — spend_id уже использован, funds — не хватает средств у приложения,
    config — проблема настроек/токена (нужен админ), user — проблема на стороне получателя."""
    m = str(msg).upper()
    if "SPEND_ID" in m or "ALREADY" in m or "DUPLICATE" in m:
        return "dup"
    if "COMMENT" in m:
        return "unknown_api"
    if any(x in m for x in ("INSUFFICIENT", "NOT_ENOUGH", "NOT ENOUGH", "COINS", "FUNDS", "BALANCE")):
        return "funds"
    if any(x in m for x in ("METHOD_DISABLED", "UNAUTHORIZED", "ACCESS_DENIED", "TOKEN", "CURRENCY",
                            "AMOUNT", "PARAM", "APP", "FORBIDDEN", "DISABLED")):
        return "config"
    if any(x in m for x in ("USER", "PEER", "BLOCKED", "NOT_FOUND", "START", "CHAT")):
        return "user"
    return "config"


PROV_NAME = {"cryptobot": "💎 CryptoBot"}


def dep_providers():
    return ["cryptobot"] if Sb("dep_cb_on") and cb_token() else []


def wd_providers():
    return ["cryptobot"] if Sb("wd_cb_on") and cb_token() else []


def fee_pct(provider) -> Decimal:
    return Sd("fee_cryptobot")


def invoice_ttl():
    return max(5, Sn("invoice_ttl_min"))


async def create_invoice(provider, uid, cents):
    if provider != "cryptobot":
        raise GameError("Этот способ оплаты недоступен")
    if not cb_token():
        raise GameError("CryptoBot отключён")
    amount = Decimal(cents) / 100
    ttl = invoice_ttl() * 60
    try:
        data = await http_json(
            "POST", f"{CRYPTO_API}/createInvoice", headers=cb_headers(),
            json={"asset": "USDT", "amount": f"{amount:.2f}", "payload": str(uid),
                  "description": f"Пополнение {S('casino_name')}, ID {uid}"[:1024], "expires_in": ttl},
        )
    except ProviderUnknown:
        raise GameError("Платёжная система временно недоступна. Попробуйте позже.")
    if data.get("ok"):
        res = data["result"]
        return f"cb:{res['invoice_id']}", res.get("bot_invoice_url") or res.get("pay_url")
    raise GameError(cb_err(data))


async def check_invoice(invoice_id):
    try:
        prov, ext = invoice_id.split(":", 1)
        if prov != "cb":
            return None
        data = await http_json("GET", f"{CRYPTO_API}/getInvoices", retries=1,
                               headers=cb_headers(), params={"invoice_ids": ext})
        items = data["result"]["items"] if data.get("ok") else []
        return items[0]["status"].lower() if items else None
    except Exception:
        log.warning("check_invoice failed for %s", invoice_id, exc_info=True)
        return None


def settle_invoice(invoice_id):
    with tx() as c:
        inv = c.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        if not inv or inv["status"] not in ("PENDING", "EXPIRED"):
            return None
        fee = fee_pct(inv["provider"])
        net = int(Decimal(inv["amount_cents"]) * (1 - fee / 100))
        c.execute("UPDATE invoices SET status='PAID', paid_at=? WHERE invoice_id=?", (now(), invoice_id))
        b = c.execute("SELECT balance FROM balances WHERE telegram_id=?", (inv["user_id"],)).fetchone()
        if not b:
            return None
        c.execute(
            "UPDATE balances SET balance=balance+?, updated_at=? WHERE telegram_id=?",
            (net, now(), inv["user_id"]),
        )
        add_ledger(c, inv["user_id"], net, b["balance"], b["balance"] + net,
                   f"депозит {inv['provider']} (комиссия {fee}%)", None, None, f"dep:{invoice_id}")
        return inv["user_id"], net, inv["amount_cents"]


async def process_invoice(invoice_id, notify=True):
    st = await check_invoice(invoice_id)
    if st == "paid":
        res = settle_invoice(invoice_id)
        if res and notify:
            await notify_user(res[0], f"✅ <b>Депозит зачислен!</b>\n{SEP}\n💰 Зачислено: <b>{money(res[1])}</b>")
        if res:
            for a in all_admin_ids():
                await notify_user(a, f"📥 Депозит: <code>{res[0]}</code> · {money(res[2])} (зачислено {money(res[1])})")
    elif st == "expired":
        ex("UPDATE invoices SET status='EXPIRED' WHERE invoice_id=? AND status='PENDING'", (invoice_id,))
    elif st == "active":
        inv = q1("SELECT created_at FROM invoices WHERE invoice_id=?", (invoice_id,))
        if inv and inv["created_at"] <= since(minutes=invoice_ttl() + 10):
            ex("UPDATE invoices SET status='EXPIRED' WHERE invoice_id=? AND status='PENDING'", (invoice_id,))
    return st


# ===================== WITHDRAWALS =====================

def deposits_total(uid):
    return q1("SELECT COALESCE(SUM(amount_cents),0) s FROM invoices WHERE user_id=? AND status='PAID'", (uid,))["s"]


def wager_need(c_or_none, uid):
    dep = deposits_total(uid)
    b = q1("SELECT bonus_need FROM balances WHERE telegram_id=?", (uid,))
    return int(Decimal(dep) * Sd("wagering")) + (b["bonus_need"] if b else 0)


def wager_state(uid):
    u = get_user(uid)
    if not u:
        return 0, 0
    return wager_need(None, uid), u["total_wagered"]


def progress_bar(have, need, n=10):
    k = n if need <= 0 else min(n, int(n * have / need))
    return "▰" * k + "▱" * (n - k)


def create_withdrawal(uid, amount, provider):
    with tx() as c:
        u = c.execute("SELECT wd_blocked FROM users WHERE telegram_id=?", (uid,)).fetchone()
        b = c.execute(
            "SELECT balance, reserved_balance, total_wagered, bonus_need FROM balances WHERE telegram_id=?", (uid,)
        ).fetchone()
        if not u or not b:
            raise GameError("Пользователь не найден")
        if u["wd_blocked"]:
            raise GameError("Вывод для вашего аккаунта ограничен. Обратитесь в поддержку.")
        if provider not in wd_providers():
            raise GameError("Этот способ вывода сейчас недоступен")
        if amount < Si("min_withdraw"):
            raise GameError(f"Минимальный вывод: {money(Si('min_withdraw'))}")
        if amount > Si("max_withdraw"):
            raise GameError(f"Максимальный вывод: {money(Si('max_withdraw'))}")
        if b["balance"] - b["reserved_balance"] < amount:
            raise GameError("Недостаточно доступных средств")
        if c.execute(
            "SELECT 1 FROM withdrawals WHERE user_id=? AND status IN('PENDING','PROCESSING','UNKNOWN','REVIEW')", (uid,)
        ).fetchone():
            raise GameError("У вас уже есть активная заявка на вывод")
        last = c.execute(
            "SELECT created_at FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)
        ).fetchone()
        cd = Sn("wd_cooldown_min")
        if cd > 0 and last and last["created_at"] > since(minutes=cd):
            raise GameError(f"Заявки можно создавать не чаще раза в {cd} мин.")
        day = c.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM withdrawals WHERE user_id=? AND status!='FAILED' AND created_at >= ?",
            (uid, since(days=1)),
        ).fetchone()["s"]
        if day + amount > Si("daily_withdraw_limit"):
            raise GameError(f"Дневной лимит вывода: {money(Si('daily_withdraw_limit'))}")
        dep = c.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM invoices WHERE user_id=? AND status='PAID'", (uid,)
        ).fetchone()["s"]
        need = int(Decimal(dep) * Sd("wagering")) + b["bonus_need"]
        if b["total_wagered"] < need:
            raise GameError(f"Для вывода нужно поставить ещё {money(need - b['total_wagered'])}")
        manual = (not Sb("auto_withdraw")) or amount > Si("auto_wd_max")
        status = "REVIEW" if manual else "PENDING"
        cur = c.execute(
            "INSERT INTO withdrawals(user_id, amount_cents, address, provider, status, created_at) VALUES(?,?,?,?,?,?)",
            (uid, amount, f"telegram:{uid}", provider, status, now()),
        )
        c.execute("UPDATE balances SET reserved_balance=reserved_balance+? WHERE telegram_id=?", (amount, uid))
        return cur.lastrowid, status


def finalize_withdrawal(wid, ok, admin=None, tx_id=None, from_states=("PROCESSING",)):
    with tx() as c:
        w = c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not w or w["status"] not in from_states:
            return None
        amt, uid = w["amount_cents"], w["user_id"]
        if ok:
            b = c.execute("SELECT balance FROM balances WHERE telegram_id=?", (uid,)).fetchone()
            c.execute(
                "UPDATE balances SET balance=balance-?, reserved_balance=MAX(0,reserved_balance-?), updated_at=? WHERE telegram_id=?",
                (amt, amt, now(), uid),
            )
            add_ledger(c, uid, -amt, b["balance"], b["balance"] - amt, f"вывод #{wid}", admin, None, f"wd:{wid}")
            c.execute("UPDATE withdrawals SET status='DONE', tx_id=?, processed_at=? WHERE id=?", (tx_id, now(), wid))
        else:
            c.execute(
                "UPDATE balances SET reserved_balance=MAX(0,reserved_balance-?) WHERE telegram_id=?", (amt, uid)
            )
            c.execute("UPDATE withdrawals SET status='FAILED', processed_at=? WHERE id=?", (now(), wid))
        return dict(w)


async def cryptobot_find_transfer(spend_id):
    """Ищет уже выполненный перевод по spend_id (идемпотентность). Возвращает transfer_id или None."""
    try:
        data = await http_json("GET", f"{CRYPTO_API}/getTransfers", retries=1,
                               headers=cb_headers(), params={"spend_id": spend_id})
    except Exception:
        return None
    if isinstance(data, dict) and data.get("ok"):
        result = data.get("result") or {}
        items = result.get("items", []) if isinstance(result, dict) else []
        for it in items:
            if str(it.get("spend_id")) == str(spend_id) and str(it.get("status", "")).lower() == "completed":
                return str(it.get("transfer_id", spend_id))
    return None


async def cryptobot_create_check(uid, cents):
    """Создаёт одноразовый CryptoBot-чек, закреплённый за Telegram ID получателя.
    Запрос намеренно не повторяется автоматически: createCheck не даёт безопасного
    spend_id для идемпотентности, а повтор после таймаута может создать второй чек.
    """
    amount = Decimal(cents) / 100
    try:
        data = await http_json(
            "POST", f"{CRYPTO_API}/createCheck", headers=cb_headers(),
            json={
                "asset": "USDT",
                "amount": f"{amount:.2f}",
                "pin_to_user_id": int(uid),
            },
        )
    except ProviderUnknown as e:
        raise ProviderUnknown(
            f"Не удалось подтвердить результат создания чека; автоматический повтор запрещён: {e}"
        ) from e

    if isinstance(data, dict) and data.get("ok"):
        result = data.get("result") or {}
        url = result.get("bot_check_url")
        if url:
            return str(url)
        raise ProviderUnknown("CryptoBot создал ответ без bot_check_url; требуется ручная сверка")

    if isinstance(data, dict) and data.get("error"):
        raise GameError(cb_err(data))
    raise ProviderUnknown("CryptoBot вернул пустой или неизвестный ответ при создании чека")


async def cryptobot_transfer(uid, cents, spend_id):
    """Перевод USDT пользователю через Crypto Pay API. spend_id делает запрос идемпотентным,
    поэтому при сетевых сбоях безопасно повторять."""
    amount = Decimal(cents) / 100
    last = None
    for attempt in range(3):
        try:
            data = await http_json(
                "POST", f"{CRYPTO_API}/transfer", headers=cb_headers(),
                json={"user_id": int(uid), "asset": "USDT", "amount": f"{amount:.2f}",
                      "spend_id": str(spend_id)},
            )
        except ProviderUnknown as e:
            last = e
            await asyncio.sleep(2 + attempt * 2)
            continue
        if isinstance(data, dict) and data.get("ok"):
            res = data.get("result") or {}
            return str(res.get("transfer_id", spend_id))
        if isinstance(data, dict) and data.get("error"):
            msg = cb_err(data)
            if cb_error_kind(msg) == "dup":
                found = await cryptobot_find_transfer(spend_id)
                if found:
                    return found
                raise ProviderUnknown(f"CryptoBot: {msg}, перевод не найден")
            raise GameError(msg)
        last = ProviderUnknown("CryptoBot: пустой ответ")
        await asyncio.sleep(2)
    raise ProviderUnknown(str(last) if last else "CryptoBot: нет ответа")


def all_admin_ids():
    return set(OWNER_IDS) | set(ADMINS)


async def notify_user(uid, text, markup=None):
    try:
        await bot.send_message(uid, text, reply_markup=markup)
    except Exception:
        pass


async def notify_admins(text, markup=None):
    for a in all_admin_ids():
        await notify_user(a, text, markup)


# ===================== BOT / FSM / KEYBOARDS =====================

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
adm = Router()
deny = Router()


class St(StatesGroup):
    dep_amount = State()
    wd_amount = State()
    stake_custom = State()
    seed_set = State()
    promo = State()


class Adm(StatesGroup):
    setting_value = State()
    game_value = State()
    user_search = State()
    user_amount = State()
    user_field = State()
    user_msg = State()
    bcast_text = State()
    promo_new = State()
    admin_add = State()


class AdminF(Filter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and is_admin(u.id))


adm.message.filter(AdminF())
adm.callback_query.filter(AdminF())


def kb(rows):
    out_ = []
    for row in rows:
        out_.append([
            InlineKeyboardButton(text=t, url=d) if d.startswith(("http://", "https://", "tg://"))
            else InlineKeyboardButton(text=t, callback_data=d)
            for t, d in row
        ])
    return InlineKeyboardMarkup(inline_keyboard=out_)


def reply_kb(admin=False):
    rows = [
        [KeyboardButton(text="🎮 Игры"), KeyboardButton(text="💰 Баланс")],
        [KeyboardButton(text="💳 Депозит"), KeyboardButton(text="💸 Вывод")],
        [KeyboardButton(text="🎁 Бонус"), KeyboardButton(text="👤 Кабинет")],
    ]
    if admin:
        rows.append([KeyboardButton(text="👑 Админ")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def out(target: Message, edit: bool, text, markup=None):
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
        except Exception:
            pass
    await target.answer(text, reply_markup=markup)


class Guard(BaseMiddleware):
    def __init__(self):
        self.last = {}

    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if not u or u.is_bot:
            return await handler(event, data)

        admin = is_admin(u.id)
        lim = Sn("throttle_ms") / 1000
        t = time.monotonic()
        if lim > 0 and not admin and t - self.last.get(u.id, 0) < lim:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except Exception:
                    pass
            return
        self.last[u.id] = t
        if len(self.last) > 5000:
            cut = t - 60
            self.last = {k: v for k, v in self.last.items() if v > cut}

        ref = None
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            p = event.text.split()
            if len(p) > 1 and p[1].isdigit():
                ref = int(p[1])
        ensure_user(u.id, u.username or "", u.full_name, ref)

        if not admin and user_status(u.id) == "BANNED":
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Аккаунт заблокирован.", show_alert=True)
            else:
                await event.answer("🚫 Ваш аккаунт заблокирован.")
            return

        if not admin and Sb("maintenance"):
            txt = S("maintenance_text")
            if isinstance(event, CallbackQuery):
                await event.answer(txt[:190], show_alert=True)
            else:
                await event.answer(txt)
            return

        return await handler(event, data)


STAKE_FALLBACK = [1000, 5000, 10000, 50000, 100000]


# ===================== MAIN SCREENS =====================

async def show_home(m: Message, uid, name):
    u = get_user(uid)
    news = S("news_url")
    rows = [
        [("🎮 Играть", "menu:games"), ("💳 Пополнить", "menu:deposit")],
        [("🎁 Бонус", "menu:bonus"), ("👤 Профиль", "menu:cab")],
    ]
    if news.startswith("http"):
        rows.append([("📣 Новости", news)])
    await m.answer(
        f"🎩 <b>{esc(S('casino_name')).upper()}</b>\n{SEP}\n"
        f"👋 Привет, <b>{esc(name)}</b>!\n\n"
        f"💰 Баланс: <b>{money(avail_of(u))}</b>\n"
        f"🏆 Сыграно: <b>{count_rounds(uid)}</b> раундов\n"
        f"💎 Статус: <b>{esc(u['vip_level'])}</b>\n\n"
        f"{S('welcome_text')}",
        reply_markup=kb(rows),
    )


async def show_games(t: Message, edit):
    rows, parts, btns = [], [], []
    for k, (emoji, _, desc) in GAMES.items():
        g = gs(k)
        if not g["enabled"]:
            continue
        parts.append(f"<b>{gtitle(k)}</b>{rtp_tag(k)}\n<i>{desc}</i>")
        btns.append((gtitle(k), f"game:{k}"))
    rows = chunk(btns, 2)
    body = "\n\n".join(parts) or "Все игры временно отключены."
    await out(t, edit, f"🎮 <b>ИГРЫ</b>\n{SEP}\n{body}", kb(rows))


async def show_balance(t: Message, edit, uid):
    u = get_user(uid)
    res = f"🔒 В выводе: <b>{money(u['reserved_balance'])}</b>\n" if u["reserved_balance"] else ""
    await out(
        t, edit,
        f"💰 <b>БАЛАНС</b>\n{SEP}\n"
        f"💰 Доступно: <b>{money(avail_of(u))}</b>\n{res}"
        f"🎁 Реф. бонусы: <b>{money(u['ref_balance'])}</b>\n"
        f"📊 Общий оборот: <b>{money(u['total_wagered'])}</b>\n"
        f"💎 Статус: <b>{esc(u['vip_level'])}</b>",
        kb([
            [("💳 Пополнить", "menu:deposit"), ("💸 Вывести", "menu:withdraw")],
            [("📜 История", "hist:0"), ("🧾 Операции", "ops:0")],
        ]),
    )


async def show_deposit(t: Message, edit):
    if not dep_providers():
        return await out(t, edit, "💳 Пополнение временно недоступно.", kb([[("⬅️ Меню", "menu:home")]]))
    lo, hi = Si("min_deposit"), Si("max_deposit")
    amts = [a for a in Lc("dep_quick") if lo <= a <= hi]
    rows = chunk([(f"{usdt(a)} USDT", f"dp:{a}") for a in amts], 3)
    rows.append([("💰 Своя сумма", "dp:c")])
    fees = " · ".join(f"{PROV_NAME[p]} {fee_pct(p)}%" for p in dep_providers())
    await out(
        t, edit,
        f"💳 <b>ДЕПОЗИТ</b>\n{SEP}\n"
        f"Мин: <b>{money(lo)}</b> · Макс: <b>{money(hi)}</b>\n"
        f"Комиссия: {fees}\n"
        f"⚡ Зачисление автоматическое после оплаты.\n\nВыберите сумму:",
        kb(rows),
    )


async def show_withdraw(t: Message, edit, uid):
    u = get_user(uid)
    need, have = wager_state(uid)
    avail = avail_of(u)
    wag = (
        f"\n🎯 Оборот: {progress_bar(have, need)} <b>{usdt(min(have, need))}/{usdt(need)} USDT</b>"
        if have < need else "\n🎯 Требование по обороту выполнено ✅"
    )
    lo, hi = Si("min_withdraw"), Si("max_withdraw")
    quick = [a for a in (1000, 5000, 10000) if lo <= a <= hi]
    rows = [[(f"{usdt(a)} USDT", f"wd:{a}") for a in quick]] if quick else []
    rows.append([("💸 Всё доступное", "wd:all"), ("💰 Своя сумма", "wd:c")])
    rows.append([("📋 Мои заявки", "mywd")])
    lim = f"\n🔒 Ваш вывод ограничен администрацией." if u["wd_blocked"] else ""
    await out(
        t, edit,
        f"💸 <b>ВЫВОД</b>\n{SEP}\n"
        f"Доступно: <b>{money(avail)}</b>\nМин: <b>{money(lo)}</b> · Макс: <b>{money(hi)}</b>{wag}{lim}\n\n"
        f"Выберите сумму:",
        kb(rows),
    )


async def show_cabinet(t: Message, edit, uid):
    u = get_user(uid)
    await out(
        t, edit,
        f"👤 <b>КАБИНЕТ</b>\n{SEP}\n"
        f"🆔 ID: <code>{uid}</code>\n👤 Имя: <b>{esc(u['name'])}</b>\n💎 Статус: <b>{esc(u['vip_level'])}</b>\n"
        f"💰 Баланс: <b>{money(avail_of(u))}</b>\n"
        f"📊 Оборот: <b>{money(u['total_wagered'])}</b>\n🏆 Раундов: <b>{count_rounds(uid)}</b>",
        kb([
            [("📜 История игр", "hist:0"), ("🏆 Топ игроков", "top")],
            [("👥 Рефералы", "refs"), ("🔑 Client Seed", "cseed")],
            [("🎟 Промокод", "promo"), ("📖 Правила", "rules")],
            [("🛡 Честная игра", "fairpage"), ("💬 Поддержка", "support")],
        ]),
    )


# ===================== NAV =====================

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("👇 Используйте кнопки меню внизу.", reply_markup=reply_kb(is_admin(m.from_user.id)))
    await show_home(m, m.from_user.id, m.from_user.full_name)


@dp.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext):
    await state.clear()
    await show_home(m, m.from_user.id, m.from_user.full_name)


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"🆔 Ваш ID: <code>{m.from_user.id}</code>")


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        f"ℹ️ <b>Команды</b>\n{SEP}\n/start — главное меню\n/menu — меню\n/promo КОД — активировать промокод\n"
        f"/bonus — ежедневный бонус\n/fair — как проверить честность\n/id — ваш ID\n\nПоддержка: {esc(S('support_username'))}"
    )


@dp.message(F.text == "🎮 Игры")
async def nav_games(m: Message, state: FSMContext):
    await state.clear()
    await show_games(m, False)


@dp.message(F.text == "💰 Баланс")
async def nav_balance(m: Message, state: FSMContext):
    await state.clear()
    await show_balance(m, False, m.from_user.id)


@dp.message(F.text == "💳 Депозит")
async def nav_deposit(m: Message, state: FSMContext):
    await state.clear()
    await show_deposit(m, False)


@dp.message(F.text == "💸 Вывод")
async def nav_withdraw(m: Message, state: FSMContext):
    await state.clear()
    await show_withdraw(m, False, m.from_user.id)


@dp.message(F.text == "👤 Кабинет")
async def nav_cabinet(m: Message, state: FSMContext):
    await state.clear()
    await show_cabinet(m, False, m.from_user.id)


@dp.message(F.text == "🎁 Бонус")
@dp.message(Command("bonus"))
async def nav_bonus(m: Message, state: FSMContext):
    await state.clear()
    await show_bonus(m, False, m.from_user.id)


FAIR_TEXT = (
    f"🛡 <b>ЧЕСТНАЯ ИГРА</b>\n{SEP}\n"
    "<b>Мины, Башня, Монетка</b> — provably fair:\n"
    "1. При старте раунда сервер создаёт <code>server_seed</code> и показывает вам его SHA-256 хэш <b>до</b> игры.\n"
    "2. Результат считается детерминированно из потока HMAC-SHA256(server_seed, "
    "<code>client_seed:round_id:counter</code>): мины — перемешивание Фишера-Йетса (j = число % (i+1), i от конца), "
    "ловушки башни — число % кол-во клеток, монетка — первое число % 2 (0 — орёл).\n"
    "3. После раунда кнопка «🔍 Проверить честность» раскрывает server_seed — сверьте хэш и пересчитайте.\n"
    "4. Свой <code>client_seed</code> задаётся в Кабинете.\n\n"
    "<b>Футбол, Баскетбол, Дартс, Кубик, Слоты</b> используют анимированные эмодзи Telegram: результат "
    "определяет сервер Telegram, мы на него не влияем.\n\n"
    "<b>Как считаются коэффициенты.</b> Для исхода с вероятностью p коэффициент = RTP ÷ p. Пример: «Чёт» на кубике "
    "выпадает в 3 случаях из 6 (p = 0.5), при RTP 95% коэффициент x1.90. Для Мин и Башни множитель на каждом шаге = "
    "RTP ÷ вероятность дойти до этого шага, то есть шанс мины/ловушки не меняется, а RTP виден в списке игр. "
    "Если включена комиссия с выигрыша — она указана в описании игры."
)


@dp.message(Command("fair"))
async def cmd_fair(m: Message):
    await m.answer(FAIR_TEXT)


# ===================== MENU CALLBACKS =====================

@dp.callback_query(F.data.startswith("menu:"))
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    a = c.data.split(":")[1]
    uid = c.from_user.id
    if a == "games":
        await show_games(c.message, True)
    elif a == "deposit":
        await show_deposit(c.message, True)
    elif a == "withdraw":
        await show_withdraw(c.message, True, uid)
    elif a == "cab":
        await show_cabinet(c.message, True, uid)
    elif a == "balance":
        await show_balance(c.message, True, uid)
    elif a == "bonus":
        await show_bonus(c.message, True, uid)
    elif a == "home":
        await show_home(c.message, uid, c.from_user.full_name)
    await c.answer()


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    await c.answer(f"Поддержка: {S('support_username')}", show_alert=True)


@dp.callback_query(F.data == "rules")
async def cb_rules(c: CallbackQuery):
    await c.message.answer(f"📖 <b>ПРАВИЛА</b>\n{SEP}\n{S('rules_text')}")
    await c.answer()


@dp.callback_query(F.data == "fairpage")
async def cb_fairpage(c: CallbackQuery):
    await c.message.answer(FAIR_TEXT)
    await c.answer()


@dp.callback_query(F.data.startswith("hist:"))
async def cb_hist(c: CallbackQuery):
    try:
        page = max(0, min(int(c.data.split(":")[1]), 50))
    except ValueError:
        return await c.answer()
    rows = qa(
        "SELECT * FROM rounds WHERE user_id=? ORDER BY started_at DESC LIMIT 11 OFFSET ?",
        (c.from_user.id, page * 10),
    )
    chunk_ = rows[:10]
    icon = {"WON": "🟢", "CASHED_OUT": "🟢", "LOST": "🔴", "ACTIVE": "⏳", "REFUNDED": "↩️"}
    body = "\n".join(
        f"{icon.get(r['state'], '⚪')} {gtitle(r['game']) if r['game'] in GAMES else r['game']} · "
        f"<b>{usdt(r['stake'])}</b> → <b>{usdt(r['payout'])}</b>"
        for r in chunk_
    ) or "Раундов пока нет."
    nav = []
    if page > 0:
        nav.append(("⬅️", f"hist:{page - 1}"))
    if len(rows) > 10:
        nav.append(("➡️", f"hist:{page + 1}"))
    await out(c.message, True, f"📜 <b>ИСТОРИЯ ИГР</b> (стр. {page + 1})\n{SEP}\n{body}",
              kb([nav] if nav else []))
    await c.answer()


@dp.callback_query(F.data.startswith("ops:"))
async def cb_ops(c: CallbackQuery):
    try:
        page = max(0, min(int(c.data.split(":")[1]), 50))
    except ValueError:
        return await c.answer()
    rows = qa(
        "SELECT * FROM ledger WHERE telegram_id=? AND reason NOT LIKE 'ставка%' AND reason NOT LIKE 'выплата%' "
        "ORDER BY id DESC LIMIT 11 OFFSET ?",
        (c.from_user.id, page * 10),
    )
    body = "\n".join(
        f"{'➕' if r['delta'] > 0 else '➖'} <b>{usdt(abs(r['delta']))}</b> · {esc(r['reason'])[:40]} · {r['created_at'][5:16].replace('T', ' ')}"
        for r in rows[:10]
    ) or "Операций пока нет."
    nav = []
    if page > 0:
        nav.append(("⬅️", f"ops:{page - 1}"))
    if len(rows) > 10:
        nav.append(("➡️", f"ops:{page + 1}"))
    await out(c.message, True, f"🧾 <b>ОПЕРАЦИИ</b>\n{SEP}\n{body}", kb([nav] if nav else []))
    await c.answer()


@dp.callback_query(F.data == "top")
async def cb_top(c: CallbackQuery):
    rows = qa(
        "SELECT u.name, u.vip_level, b.total_wagered FROM users u JOIN balances b USING(telegram_id) "
        "WHERE u.status!='BANNED' ORDER BY b.total_wagered DESC LIMIT 10"
    )
    medals = ["🥇", "🥈", "🥉"]
    body = "\n".join(
        f"{medals[i] if i < 3 else f'{i + 1}.'} {esc(r['name'])} · <i>{esc(r['vip_level'])}</i> · <b>{usdt(r['total_wagered'])}</b>"
        for i, r in enumerate(rows)
    ) or "Рейтинг пуст."
    await out(c.message, True, f"🏆 <b>ТОП ИГРОКОВ</b> (по обороту)\n{SEP}\n{body}", kb([[("⬅️ Кабинет", "menu:cab")]]))
    await c.answer()


@dp.callback_query(F.data == "refs")
async def cb_refs(c: CallbackQuery):
    uid = c.from_user.id
    me = await bot.get_me()
    n = q1("SELECT COUNT(*) n FROM users WHERE referrer_id=?", (uid,))["n"]
    u = get_user(uid)
    on = Sb("ref_enabled")
    basis = "прибыли" if S("ref_basis") == "win" else "ставок"
    await out(
        c.message, True,
        f"👥 <b>РЕФЕРАЛЫ</b>\n{SEP}\n"
        + (f"Получайте <b>{Sd('ref_percent')}%</b> от {basis} приглашённых друзей!\n\n" if on else "Программа временно отключена.\n\n")
        + f"🔗 Ваша ссылка:\n<code>https://t.me/{me.username}?start={uid}</code>\n"
        f"👥 Приглашено: <b>{n}</b>\n🎁 Заработано: <b>{money(u['ref_balance'])}</b>",
        kb([[("⬅️ Кабинет", "menu:cab")]]),
    )
    await c.answer()


@dp.callback_query(F.data == "cseed")
async def cb_cseed(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.seed_set)
    await c.message.answer(
        f"🔑 Ваш Client Seed: <code>{esc(get_client_seed(c.from_user.id))}</code>\n\n"
        "Отправьте новый (3–32 символа: буквы, цифры, _ -) или нажмите любую кнопку меню для отмены."
    )
    await c.answer()


@dp.message(St.seed_set, F.text)
async def fsm_seed(m: Message, state: FSMContext):
    seed = m.text.strip()
    if not re.fullmatch(r"[\w-]{3,32}", seed):
        return await m.answer("⚠️ Допустимо 3–32 символа: буквы, цифры, _ и -")
    ex(
        "INSERT INTO user_seeds(user_id, client_seed) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET client_seed=excluded.client_seed",
        (m.from_user.id, seed),
    )
    await state.clear()
    await m.answer(f"✅ Client Seed: <code>{esc(seed)}</code>")


# ===================== BONUSES / PROMO =====================

def grant_bonus(c, uid, amount, reason, key):
    b = c.execute("SELECT balance FROM balances WHERE telegram_id=?", (uid,)).fetchone()
    need = int(Decimal(amount) * Sd("bonus_wager"))
    c.execute(
        "UPDATE balances SET balance=balance+?, bonus_need=bonus_need+?, updated_at=? WHERE telegram_id=?",
        (amount, need, now(), uid),
    )
    add_ledger(c, uid, amount, b["balance"], b["balance"] + amount, reason, None, None, key)
    return need


def daily_status(uid):
    u = get_user(uid)
    cd = max(1, Sn("daily_bonus_cd_h"))
    if not Sb("daily_bonus_on") or Si("daily_bonus_amount") <= 0:
        return "off", None
    if deposits_total(uid) < Si("daily_bonus_min_dep"):
        return "dep", Si("daily_bonus_min_dep")
    if u["last_daily"]:
        nxt = datetime.fromisoformat(u["last_daily"]) + timedelta(hours=cd)
        if nxt > datetime.now(timezone.utc):
            return "wait", nxt
    return "ready", None


async def show_bonus(t: Message, edit, uid):
    st, info = daily_status(uid)
    amt = Si("daily_bonus_amount")
    wag = Sd("bonus_wager")
    if st == "off":
        body = "Ежедневный бонус сейчас недоступен."
        rows = []
    elif st == "dep":
        body = f"Для получения бонуса нужны депозиты от <b>{money(info)}</b>."
        rows = [[("💳 Пополнить", "menu:deposit")]]
    elif st == "wait":
        left = info - datetime.now(timezone.utc)
        h, rem = divmod(int(left.total_seconds()), 3600)
        body = f"⏳ Следующий бонус через <b>{h} ч {rem // 60} мин</b>."
        rows = []
    else:
        body = f"🎁 Бонус <b>{money(amt)}</b> готов к получению!\nВейджер бонуса: x{wag}."
        rows = [[("🎁 Забрать бонус", "claim")]]
    if Sb("promo_on"):
        rows.append([("🎟 Ввести промокод", "promo")])
    rows.append([("⬅️ Меню", "menu:home")])
    await out(t, edit, f"🎁 <b>БОНУСЫ</b>\n{SEP}\n{body}", kb(rows))


@dp.callback_query(F.data == "claim")
async def cb_claim(c: CallbackQuery):
    uid = c.from_user.id
    st, _ = daily_status(uid)
    if st != "ready":
        await c.answer("Бонус пока недоступен", show_alert=True)
        return await show_bonus(c.message, True, uid)
    amt = Si("daily_bonus_amount")
    with tx() as cx:
        r = cx.execute("SELECT last_daily FROM users WHERE telegram_id=?", (uid,)).fetchone()
        cd = max(1, Sn("daily_bonus_cd_h"))
        if r["last_daily"] and datetime.fromisoformat(r["last_daily"]) + timedelta(hours=cd) > datetime.now(timezone.utc):
            return await c.answer("Бонус уже получен", show_alert=True)
        cx.execute("UPDATE users SET last_daily=? WHERE telegram_id=?", (now(), uid))
        grant_bonus(cx, uid, amt, "ежедневный бонус", f"daily:{uid}:{now()}")
    await c.answer(f"🎁 +{usdt(amt)} USDT", show_alert=True)
    await show_bonus(c.message, True, uid)


def redeem_promo(uid, code):
    code = code.strip().upper()
    with tx() as c:
        p = c.execute("SELECT * FROM promos WHERE code=?", (code,)).fetchone()
        if not p or not p["active"]:
            raise GameError("Промокод не найден или отключён")
        if p["used"] >= p["max_uses"]:
            raise GameError("Лимит активаций промокода исчерпан")
        if c.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)).fetchone():
            raise GameError("Вы уже активировали этот промокод")
        c.execute("INSERT INTO promo_uses(code,user_id,created_at) VALUES(?,?,?)", (code, uid, now()))
        c.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,))
        grant_bonus(c, uid, p["amount"], f"промокод {code}", f"promo:{code}:{uid}")
        return p["amount"]


@dp.callback_query(F.data == "promo")
async def cb_promo(c: CallbackQuery, state: FSMContext):
    if not Sb("promo_on"):
        return await c.answer("Промокоды отключены", show_alert=True)
    await state.set_state(St.promo)
    await c.message.answer("🎟 Отправьте промокод:")
    await c.answer()


@dp.message(St.promo, F.text)
async def fsm_promo(m: Message, state: FSMContext):
    await state.clear()
    try:
        amt = redeem_promo(m.from_user.id, m.text)
        await m.answer(f"✅ Промокод активирован! Начислено <b>{money(amt)}</b>.")
    except GameError as e:
        await m.answer(f"⚠️ {e}")


@dp.message(Command("promo"))
async def cmd_promo(m: Message):
    parts = (m.text or "").split()
    if not Sb("promo_on"):
        return await m.answer("Промокоды отключены.")
    if len(parts) != 2:
        return await m.answer("Формат: <code>/promo КОД</code>")
    try:
        amt = redeem_promo(m.from_user.id, parts[1])
        await m.answer(f"✅ Промокод активирован! Начислено <b>{money(amt)}</b>.")
    except GameError as e:
        await m.answer(f"⚠️ {e}")


# ===================== DEPOSIT =====================

async def deposit_pick_provider(t: Message, edit, cents):
    provs = dep_providers()
    if not provs:
        return await out(t, edit, "💳 Пополнение временно недоступно.")
    btns = [(f"{PROV_NAME[p]} ({fee_pct(p)}%)", f"dpp:{p}:{cents}") for p in provs]
    rows = [btns, [("⬅️ Назад", "menu:deposit")]]
    await out(t, edit, f"💳 Сумма: <b>{money(cents)}</b>\nВыберите платёжную систему:", kb(rows))


def deposit_validate(cents):
    if cents < Si("min_deposit"):
        return f"Минимальный депозит: {money(Si('min_deposit'))}"
    if cents > Si("max_deposit"):
        return f"Максимальный депозит: {money(Si('max_deposit'))}"
    return None


@dp.callback_query(F.data.startswith("dp:"))
async def cb_dp(c: CallbackQuery, state: FSMContext):
    v = c.data.split(":")[1]
    if v == "c":
        await state.set_state(St.dep_amount)
        await c.message.answer(
            f"💰 Введите сумму депозита в USDT (от {usdt(Si('min_deposit'))} до {usdt(Si('max_deposit'))}):"
        )
        return await c.answer()
    try:
        cents = int(v)
    except ValueError:
        return await c.answer()
    err = deposit_validate(cents)
    if err:
        return await c.answer(err, show_alert=True)
    await deposit_pick_provider(c.message, True, cents)
    await c.answer()


@dp.message(St.dep_amount, F.text)
async def fsm_dep_amount(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    err = "Введите число, например 25.5" if cents is None else deposit_validate(cents)
    if err:
        return await m.answer(f"⚠️ {err}")
    await state.clear()
    await deposit_pick_provider(m, False, cents)


@dp.callback_query(F.data.startswith("dpp:"))
async def cb_dpp(c: CallbackQuery):
    try:
        _, provider, cs = c.data.split(":")
        cents = int(cs)
    except ValueError:
        return await c.answer()
    uid = c.from_user.id
    err = deposit_validate(cents)
    if not err and provider not in dep_providers():
        err = "Этот способ оплаты недоступен"
    if not err:
        pend = q1("SELECT COUNT(*) n FROM invoices WHERE user_id=? AND status='PENDING'", (uid,))["n"]
        last = q1("SELECT created_at FROM invoices WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (uid,))
        day = q1(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM invoices WHERE user_id=? AND status IN('PENDING','PAID') AND created_at >= ?",
            (uid, since(days=1)),
        )["s"]
        if pend >= max(1, Sn("max_pending_invoices")):
            err = f"Не более {Sn('max_pending_invoices')} неоплаченных счетов одновременно"
        elif last and last["created_at"] > since(seconds=10):
            err = "Подождите 10 секунд перед созданием нового счёта"
        elif day + cents > Si("daily_deposit_limit"):
            err = f"Дневной лимит депозитов: {money(Si('daily_deposit_limit'))}"
    if err:
        return await c.answer(err, show_alert=True)
    await c.answer("Создаю счёт…")
    try:
        inv_id, link = await create_invoice(provider, uid, cents)
    except GameError as e:
        return await c.message.answer(f"⚠️ {e}")
    except Exception:
        log.exception("invoice create")
        return await c.message.answer("⚠️ Ошибка платёжной системы. Попробуйте позже.")
    ex(
        "INSERT INTO invoices(invoice_id, user_id, provider, amount_cents, status, created_at) VALUES(?,?,?,?,'PENDING',?)",
        (inv_id, uid, provider, cents, now()),
    )
    fee = fee_pct(provider)
    net = int(Decimal(cents) * (1 - fee / 100))
    await out(
        c.message, True,
        f"💳 <b>СЧЁТ СОЗДАН</b>\n{SEP}\nСумма: <b>{money(cents)}</b> · {PROV_NAME[provider]}\n"
        f"К зачислению: <b>{money(net)}</b>\n⏱ Действует {invoice_ttl()} минут.\n\n"
        f"Оплатите счёт — баланс пополнится <b>автоматически</b> в течение минуты. "
        f"Кнопка «Проверить» нужна только для ручной проверки.",
        kb([[("🔗 Оплатить", link)], [("🔄 Проверить оплату", f"chk:{inv_id}")]]),
    )


@dp.callback_query(F.data.startswith("chk:"))
async def cb_chk(c: CallbackQuery):
    inv_id = c.data.split(":", 1)[1]
    inv = q1("SELECT * FROM invoices WHERE invoice_id=?", (inv_id,))
    if not inv or inv["user_id"] != c.from_user.id:
        return await c.answer("Счёт не найден", show_alert=True)
    if inv["status"] == "PAID":
        return await c.answer("✅ Этот счёт уже зачислен", show_alert=True)
    st = await process_invoice(inv_id, notify=False)
    if st == "paid":
        u = get_user(c.from_user.id)
        await out(c.message, True, f"✅ <b>Депозит зачислен!</b>\nБаланс: <b>{money(avail_of(u))}</b>")
        await c.answer()
    elif st == "expired":
        await c.answer("⌛ Счёт истёк", show_alert=True)
    elif st == "active":
        await c.answer("Оплата пока не поступила. Попробуйте через минуту.", show_alert=True)
    else:
        await c.answer("Не удалось проверить статус, повторите позже.", show_alert=True)


# ===================== WITHDRAW UI =====================

def wd_precheck(uid, cents):
    u = get_user(uid)
    if u["wd_blocked"]:
        return "Вывод для вашего аккаунта ограничен. Обратитесь в поддержку."
    if not wd_providers():
        return "Вывод временно недоступен"
    if cents < Si("min_withdraw"):
        return f"Минимальный вывод: {money(Si('min_withdraw'))}"
    if cents > Si("max_withdraw"):
        return f"Максимальный вывод: {money(Si('max_withdraw'))}"
    if avail_of(u) < cents:
        return "Недостаточно доступных средств"
    need, have = wager_state(uid)
    if have < need:
        return f"Для вывода нужно поставить ещё {money(need - have)}"
    return None


async def wd_pick_provider(t: Message, edit, cents):
    btns = [(PROV_NAME[p], f"wdp:{p}:{cents}") for p in wd_providers()]
    await out(
        t, edit,
        f"💸 Вывод <b>{money(cents)}</b>\nВыберите, куда отправить (на ваш Telegram-аккаунт):",
        kb([btns, [("⬅️ Назад", "menu:withdraw")]]),
    )


@dp.callback_query(F.data.startswith("wd:"))
async def cb_wd(c: CallbackQuery, state: FSMContext):
    v, uid = c.data.split(":")[1], c.from_user.id
    if v == "c":
        await state.set_state(St.wd_amount)
        await c.message.answer(f"💰 Введите сумму вывода в USDT (мин. {usdt(Si('min_withdraw'))}):")
        return await c.answer()
    u = get_user(uid)
    try:
        cents = min(avail_of(u), Si("max_withdraw")) if v == "all" else int(v)
    except ValueError:
        return await c.answer()
    err = wd_precheck(uid, cents)
    if err:
        return await c.answer(err, show_alert=True)
    await wd_pick_provider(c.message, True, cents)
    await c.answer()


@dp.message(St.wd_amount, F.text)
async def fsm_wd_amount(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    err = "Введите число, например 15" if cents is None else wd_precheck(m.from_user.id, cents)
    if err:
        return await m.answer(f"⚠️ {err}")
    await state.clear()
    await wd_pick_provider(m, False, cents)


@dp.callback_query(F.data.startswith("wdp:"))
async def cb_wdp(c: CallbackQuery):
    try:
        _, prov, cs = c.data.split(":")
        cents = int(cs)
    except ValueError:
        return await c.answer()
    err = wd_precheck(c.from_user.id, cents)
    if not err and prov not in wd_providers():
        err = "Этот способ вывода недоступен"
    if err:
        return await c.answer(err, show_alert=True)
    manual = (not Sb("auto_withdraw")) or cents > Si("auto_wd_max")
    note = "⏳ Заявка пойдёт на проверку администратором." if manual else "⚡ Выплата будет произведена автоматически."
    await out(
        c.message, True,
        f"💸 <b>ПОДТВЕРЖДЕНИЕ</b>\n{SEP}\nВывести <b>{money(cents)}</b>\n"
        f"Куда: {PROV_NAME[prov]}, Telegram ID <code>{c.from_user.id}</code>\n{note}\n\n"
        f"⚠️ Аккаунт должен быть запущен в выбранном кошельке-боте.",
        kb([[("✅ Подтвердить", f"wdok:{prov}:{cents}"), ("❌ Отмена", "wdno")]]),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("wdok:"))
async def cb_wdok(c: CallbackQuery):
    try:
        _, prov, cs = c.data.split(":")
        cents = int(cs)
    except ValueError:
        return await c.answer()
    uid = c.from_user.id
    try:
        wid, status = create_withdrawal(uid, cents, prov)
    except GameError as e:
        await out(c.message, True, f"⚠️ {e}")
        return await c.answer()
    audit(uid, "withdraw_request", f"#{wid} {cents} {prov} {status}")
    if status == "REVIEW":
        await out(c.message, True,
                  f"📨 <b>Заявка №{wid} принята</b>\n{SEP}\nСумма: <b>{money(cents)}</b>\n"
                  f"Средства зарезервированы. Заявка на проверке у администратора.")
        await notify_admins(
            f"🔔 <b>Заявка на вывод №{wid}</b>\nИгрок: <code>{uid}</code>\nСумма: <b>{money(cents)}</b> · {prov}",
            kb([[("✅ Одобрить (авто-выплата)", f"adm:wda:{wid}"), ("❌ Отклонить", f"adm:wdr:{wid}")]]),
        )
    else:
        await out(c.message, True,
                  f"📨 <b>Заявка №{wid} принята</b>\n{SEP}\nСумма: <b>{money(cents)}</b>\n"
                  f"Средства зарезервированы. Автовыплата будет произведена в течение минуты.")
    await c.answer()


@dp.callback_query(F.data == "wdno")
async def cb_wdno(c: CallbackQuery):
    await out(c.message, True, "❌ Вывод отменён.")
    await c.answer()


@dp.callback_query(F.data == "mywd")
async def cb_mywd(c: CallbackQuery):
    rows = qa("SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 10", (c.from_user.id,))
    ic = {"PENDING": "⏳", "PROCESSING": "🔄", "DONE": "✅", "FAILED": "❌", "UNKNOWN": "⚠️", "REVIEW": "🕵️"}
    names = {"PENDING": "в очереди", "PROCESSING": "выплачивается", "DONE": "выплачено", "FAILED": "отклонено",
             "UNKNOWN": "сверка", "REVIEW": "на проверке"}
    body = "\n".join(
        f"{ic.get(r['status'], '•')} №{r['id']} · <b>{usdt(r['amount_cents'])}</b> · {names.get(r['status'], r['status'])}"
        for r in rows
    ) or "Заявок нет."
    await out(c.message, True, f"📋 <b>МОИ ЗАЯВКИ</b>\n{SEP}\n{body}", kb([[("⬅️ Вывод", "menu:withdraw")]]))
    await c.answer()


# ===================== GAMES MENU =====================

def parse_dice_param(param):
    """'исход.серия' -> (исход, n) либо None."""
    try:
        key, n = str(param).split(".")
        return key, int(n)
    except (ValueError, AttributeError):
        return None


def valid_param(game, param):
    try:
        if game == "mines":
            n = int(param)
            return str(n) if 1 <= n <= 24 else None
        if game == "tower":
            d, f = param.split("-")
            f = int(f)
            return f"{d}-{f}" if d in TOWER_CELLS and 2 <= f <= 30 else None
        if game == "coin":
            return param if param in ("h", "t") else None
        pr = parse_dice_param(param)
        if not pr:
            return None
        key, n = pr
        if not (1 <= n <= 10):
            return None
        if game == "slots":
            return f"x.{n}" if key == "x" else None
        if game in OUTCOMES and key in OUTCOMES[game][1]:
            return f"{key}.{n}"
        return None
    except (ValueError, AttributeError):
        return None


def series_rows(game, key):
    return [
        [("🎯 1 бросок", f"pick:{game}:{key}.1"), ("3 броска", f"pick:{game}:{key}.3")],
        [("5 бросков", f"pick:{game}:{key}.5"), ("10 бросков", f"pick:{game}:{key}.10")],
    ]


@dp.callback_query(F.data.startswith("game:"))
async def cb_game(c: CallbackQuery):
    g = c.data.split(":")[1]
    if g not in GAMES or not gs(g)["enabled"]:
        return await c.answer("Игра временно отключена.", show_alert=True)
    _, _, desc = GAMES[g]
    head = f"<b>{gtitle(g)}</b>{rtp_tag(g)}\n{SEP}\n{desc}\n\n"
    tail = fee_line() + cap_line()
    if g == "mines":
        opts = [n for n in Li("mines_counts") if 1 <= n <= 24] or [1, 3, 5]
        rows = chunk([(f"💣 {n}", f"pick:mines:{n}") for n in opts], 4)
        txt = head + "Выберите количество мин (больше мин — выше множитель):" + tail
    elif g == "tower":
        rows = [[(f"{'🟢' if d == 'easy' else '🟡' if d == 'std' else '🔴'} {TOWER_NAME[d]}", f"tdf:{d}")] for d in TOWER_CELLS]
        txt = head + "Выберите сложность:" + tail
    elif g == "coin":
        keys = enabled_outcomes("coin")
        rows = [[(f"{COIN_LABEL[k]} · x{outcome_coeff('coin', k):.2f}", f"pick:coin:{k}") for k in keys]] if keys else []
        txt = head + ("Выберите сторону:" if keys else "Сейчас нет доступных исходов.") + tail
    elif g == "slots":
        rows = series_rows(g, "x")
        pay = "\n".join(f"{SLOT_LABEL[k]} — <b>x{outcome_coeff('slots', k):.2f}</b>" for k in SLOT_RATIO)
        txt = head + f"Выплаты:\n{pay}\n\nРежим (серия бросков):" + tail
    else:
        keys = enabled_outcomes(g)
        rows = [[(f"{outcome_label(g, k)} · x{outcome_coeff(g, k):.2f}", f"out:{g}:{k}")] for k in keys]
        lines = "\n".join(f"{outcome_label(g, k)} — <b>x{outcome_coeff(g, k):.2f}</b>" for k in keys)
        txt = head + (f"Исходы и коэффициенты:\n{lines}\n\nВыберите исход:" if keys else "Сейчас нет доступных исходов.") + tail
    rows.append([("⬅️ Игры", "menu:games")])
    await out(c.message, True, txt, kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("out:"))
async def cb_out(c: CallbackQuery):
    try:
        _, g, key = c.data.split(":")
    except ValueError:
        return await c.answer()
    if g not in OUTCOMES or key not in OUTCOMES[g][1] or not gs(g)["enabled"]:
        return await c.answer("Игра временно отключена.", show_alert=True)
    if not outcome_enabled(g, key):
        return await c.answer("Этот исход временно отключён.", show_alert=True)
    rows = series_rows(g, key) + [[("⬅️ Назад", f"game:{g}")]]
    await out(
        c.message, True,
        f"<b>{gtitle(g)}</b>{rtp_tag(g)}\n{SEP}\nИсход: <b>{outcome_label(g, key)}</b> · x{outcome_coeff(g, key):.2f}\n\n"
        f"Режим (серия бросков):{fee_line()}",
        kb(rows),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("tdf:"))
async def cb_tdf(c: CallbackQuery):
    d = c.data.split(":")[1]
    if d not in TOWER_CELLS:
        return await c.answer()
    floors = [f for f in Li("tower_floors") if 2 <= f <= 30] or [5, 8, 10]
    await out(
        c.message, True,
        f"<b>{gtitle('tower')}</b> · {TOWER_NAME[d]}\nВыберите высоту башни:",
        kb(chunk([(f"{f} этажей", f"pick:tower:{d}-{f}") for f in floors], 3) + [[("⬅️ Назад", "game:tower")]]),
    )
    await c.answer()


def stake_kb(game, param, uid):
    lo, hi = stake_bounds(game, uid)
    stakes = [s for s in Lc("stake_quick") if lo <= s <= hi] or [s for s in STAKE_FALLBACK if lo <= s <= hi]
    rows = chunk([(usdt(s), f"stk:{game}:{param}:{s}") for s in stakes], 3)
    rows.append([("💰 Своя сумма", f"stk:{game}:{param}:c")])
    if game in DICE or game == "coin":
        rows.append([("📊 Статистика", f"dst:{game}")])
    rows.append([("⬅️ Назад", f"game:{game}")])
    return kb(rows)


@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick(c: CallbackQuery):
    try:
        _, game, param = c.data.split(":")
    except ValueError:
        return await c.answer()
    if game not in GAMES or valid_param(game, param) is None:
        return await c.answer("Некорректный выбор", show_alert=True)
    lo, hi = stake_bounds(game, c.from_user.id)
    extra = ""
    if game == "mines":
        extra = f"💣 Мин: <b>{param}</b>\n"
    elif game == "tower":
        d, f = param.split("-")
        extra = f"🏙 Этажей: <b>{f}</b> · {TOWER_NAME[d]}\n"
    elif game == "coin":
        if not outcome_enabled("coin", param):
            return await c.answer("Этот исход временно отключён.", show_alert=True)
        extra = f"Ваш выбор: <b>{COIN_LABEL[param]}</b> · x{outcome_coeff('coin', param):.2f}\n"
    else:
        key, n = parse_dice_param(param)
        if game != "slots":
            if not outcome_enabled(game, key):
                return await c.answer("Этот исход временно отключён.", show_alert=True)
            extra = f"Исход: <b>{outcome_label(game, key)}</b> · x{outcome_coeff(game, key):.2f}\n"
        if n > 1:
            extra += f"🔁 Серия: <b>{n} бросков</b>\n"
    await out(
        c.message, True,
        f"<b>{gtitle(game)}</b>{rtp_tag(game)}\n{SEP}\n{extra}Ставка: от <b>{money(lo)}</b> до <b>{money(hi)}</b>\n\nВыберите ставку:",
        stake_kb(game, param, c.from_user.id),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("dst:"))
async def cb_dst(c: CallbackQuery):
    g = c.data.split(":")[1]
    if g not in GAMES:
        return await c.answer()
    r = q1(
        "SELECT COUNT(*) n, SUM(state='WON') w FROM rounds WHERE user_id=? AND game=? AND state IN('WON','LOST')",
        (c.from_user.id, g),
    )
    n, w = r["n"], r["w"] or 0
    await c.answer(
        f"{gtitle(g)}\nРаундов: {n}\nПобед: {w} ({(w / n * 100) if n else 0:.1f}%)",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("stk:"))
async def cb_stk(c: CallbackQuery, state: FSMContext):
    try:
        _, game, param, v = c.data.split(":")
    except ValueError:
        return await c.answer()
    if game not in GAMES or valid_param(game, param) is None:
        return await c.answer("Некорректный выбор", show_alert=True)
    if v == "c":
        lo, hi = stake_bounds(game, c.from_user.id)
        await state.set_state(St.stake_custom)
        await state.update_data(game=game, param=param)
        await c.message.answer(f"💰 Введите ставку в USDT (от {usdt(lo)} до {usdt(hi)}):")
        return await c.answer()
    try:
        stake = int(v)
    except ValueError:
        return await c.answer()
    await begin_game(c.message, c.from_user.id, c.from_user.full_name, game, param, stake, c, True)


@dp.message(St.stake_custom, F.text)
async def fsm_stake(m: Message, state: FSMContext):
    d = await state.get_data()
    cents = parse_money(m.text)
    lo, hi = stake_bounds(d["game"], m.from_user.id)
    if cents is None or not (lo <= cents <= hi):
        return await m.answer(f"⚠️ Введите число от {usdt(lo)} до {usdt(hi)}")
    await state.clear()
    await begin_game(m, m.from_user.id, m.from_user.full_name, d["game"], d["param"], cents, None, False)


@dp.callback_query(F.data.startswith("again:"))
async def cb_again(c: CallbackQuery):
    r = get_round(c.data.split(":")[1])
    if not r or r["user_id"] != c.from_user.id:
        return await c.answer("Раунд не найден", show_alert=True)
    data = json.loads(r["data_json"])
    await begin_game(c.message, c.from_user.id, c.from_user.full_name, r["game"],
                     str(data.get("param", "1")), r["stake"], c, False)


@dp.callback_query(F.data.startswith("resume:"))
async def cb_resume(c: CallbackQuery):
    r = get_round(c.data.split(":")[1])
    if not r or r["user_id"] != c.from_user.id or r["state"] != "ACTIVE" or r["game"] not in ("mines", "tower"):
        return await c.answer("Раунд недоступен", show_alert=True)
    text, markup = render_round(r)
    await out(c.message, True, text, markup)
    await c.answer()


async def warn(cq, t: Message, text):
    if cq:
        await cq.answer(text, show_alert=True)
    else:
        await t.answer(f"⚠️ {text}")


async def begin_game(t: Message, uid, name, game, param, stake, cq, edit):
    if game not in GAMES or not gs(game)["enabled"]:
        return await warn(cq, t, "Игра временно отключена.")
    param = valid_param(game, param)
    if param is None:
        return await warn(cq, t, "Некорректные параметры игры.")
    okey = param if game == "coin" else (param.split(".")[0] if game in OUTCOMES else None)
    if okey is not None and not outcome_enabled(game, okey):
        return await warn(cq, t, "Этот исход временно отключён.")
    lo, hi = stake_bounds(game, uid)
    if not (lo <= stake <= hi):
        return await warn(cq, t, f"Ставка должна быть от {money(lo)} до {money(hi)}")
    u = get_user(uid)
    if not u or avail_of(u) < stake:
        return await warn(cq, t, "Недостаточно средств. Пополните баланс.")

    act = active_round(uid)
    if act or uid in busy_users:
        if act and act["game"] in ("mines", "tower"):
            if cq:
                await cq.answer()
            return await t.answer(
                "⛔ <b>У вас есть незавершённая игра.</b>\nЗавершите её или дождитесь автозакрытия.",
                reply_markup=kb([[("▶️ Продолжить игру", f"resume:{act['round_id']}")]]),
            )
        return await warn(cq, t, "У вас есть активный раунд. Подождите окончания.")

    if cq:
        await cq.answer()
    rid = secrets.token_hex(8)

    try:
        if game in ("mines", "tower"):
            ss = secrets.token_hex(32)
            cs = get_client_seed(uid)
            rtp = game_rtp(game)
            if game == "mines":
                n = int(param)
                data = {"mines": pf_shuffle(25, ss, cs, rid)[:n], "opened": [], "step": 0, "mcount": n, "param": param}
            else:
                diff, floors = param.split("-")
                floors = int(floors)
                cells = TOWER_CELLS[diff]
                data = {"danger": pf_traps(floors, cells, ss, cs, rid), "floor": 0, "floors": floors,
                        "cells": cells, "diff": diff, "param": param}
            start_round(rid, uid, game, stake, rtp, data, ss, cs)
            text, markup = render_round(get_round(rid))
            await out(t, edit, text, markup)
        elif game == "coin":
            await play_coin(t, uid, param, stake)
        else:
            await play_dice(t, uid, game, param, stake)
    except GameError as e:
        await t.answer("⛔ У вас уже есть активный раунд." if str(e) == "active" else f"⚠️ {e}")
    except Exception:
        log.exception("game start failed")
        await t.answer("⚠️ Ошибка запуска игры. Если списались средства — они будут возвращены автоматически.")


# ===================== MINES / TOWER =====================

def mines_mult(rtp, mcount, step) -> Decimal:
    mult = Decimal(1)
    if step <= 0:
        return mult
    mult = Decimal(rtp)
    for i in range(step):
        mult *= Decimal(25 - i) / Decimal(25 - mcount - i)
    return mult


def tower_mult(rtp, cells, floor) -> Decimal:
    if floor <= 0:
        return Decimal(1)
    return Decimal(rtp) * (Decimal(cells) / Decimal(cells - 1)) ** floor


def round_progress(r):
    """(шагов, множитель) для активного раунда Мины/Башня."""
    data = json.loads(r["data_json"])
    if r["game"] == "mines":
        step = data["step"]
        return step, mines_mult(r["coeff"], data["mcount"], step)
    step = data["floor"]
    return step, tower_mult(r["coeff"], data["cells"], step)


def end_kb(rid, board=None):
    rows = list(board or [])
    rows.append([("🔄 Играть снова", f"again:{rid}"), ("🔍 Проверить честность", f"fair:{rid}")])
    rows.append([("🎮 Игры", "menu:games")])
    return kb(rows)


def render_round(r):
    data = json.loads(r["data_json"])
    rid = r["round_id"]
    stake = r["stake"]
    h = f"🔒 Hash: <code>{(r['server_seed_hash'] or '')[:16]}…</code>"

    if r["game"] == "mines":
        m, step = data["mcount"], data["step"]
        mult = mines_mult(r["coeff"], m, step)
        pot = int(Decimal(stake) * mult) if step else 0
        rows = [
            [("🟩" if y * 5 + x in data["opened"] else "⬜", f"mine:{rid}:{y * 5 + x}") for x in range(5)]
            for y in range(5)
        ]
        rows.append([
            (f"⚡ Забрать {usdt(pot)}" if step else "⚡ Забрать", f"cash:{rid}"),
            ("🔒 Seed", f"fair:{rid}"),
        ])
        return (
            f"💣 <b>МИНЫ</b> · {m} мин\n{SEP}\n"
            f"💵 Ставка: <b>{money(stake)}</b>\n"
            f"📈 Множитель: <b>x{mult:.2f}</b>\n"
            f"💰 Куш: <b>{money(pot)}</b>\n"
            f"🟩 Открыто: <b>{step}</b>/{25 - m}\n{SEP}\n{h}"
        ), kb(rows)

    f, total, cells = data["floor"], data["floors"], data["cells"]
    mult = tower_mult(r["coeff"], cells, f)
    nxt = tower_mult(r["coeff"], cells, f + 1)
    pot = int(Decimal(stake) * mult) if f else 0
    ladder = "".join(f"🟩 Этаж {i + 1} ✅\n" for i in range(max(0, f - 4), f))
    rows = [
        [(f"⬜ {i + 1}", f"tl:{rid}:{i}") for i in range(cells)],
        [(f"⚡ Забрать {usdt(pot)}" if f else "⚡ Забрать", f"cash:{rid}"), ("🔒 Seed", f"fair:{rid}")],
    ]
    return (
        f"🏙 <b>БАШНЯ</b> · этаж {f + 1}/{total}\n{SEP}\n"
        f"💵 Ставка: <b>{money(stake)}</b>\n"
        f"📈 Множитель: <b>x{mult:.2f}</b> → следующий <b>x{nxt:.2f}</b>\n"
        f"💰 Куш: <b>{money(pot)}</b>\n{ladder}{SEP}\n{h}"
    ), kb(rows)


def valid_round(r, uid):
    return r and r["user_id"] == uid and r["state"] == "ACTIVE"


def mines_board(data, pos=None, reveal=True):
    rows = []
    for y in range(5):
        row = []
        for x in range(5):
            i = y * 5 + x
            if pos is not None and i == pos and i in data["mines"]:
                sym = "💥"
            elif reveal and i in data["mines"]:
                sym = "💣"
            elif i in data["opened"]:
                sym = "🟩"
            else:
                sym = "⬜"
            row.append((sym, "noop"))
        rows.append(row)
    return rows


@dp.callback_query(F.data.startswith("mine:"))
async def cb_mine(c: CallbackQuery):
    try:
        _, rid, ps = c.data.split(":")
        pos = int(ps)
    except ValueError:
        return await c.answer()
    if not (0 <= pos < 25):
        return await c.answer()

    async with round_locks[rid]:
        r = get_round(rid)
        if not valid_round(r, c.from_user.id) or r["game"] != "mines":
            return await c.answer("Раунд недоступен.", show_alert=True)
        data = json.loads(r["data_json"])
        if pos in data["opened"]:
            return await c.answer("Клетка уже открыта.")
        data["opened"].append(pos)
        data["step"] += 1
        m = data["mcount"]

        if pos in data["mines"]:
            set_round_data(rid, data)
            finish_round(rid, "LOST", 0, "mine")
            await out(c.message, True,
                      f"💥 <b>МИНА!</b>\n{SEP}\nПотеряно: <b>{money(r['stake'])}</b>\nОткрыто клеток: {data['step'] - 1}",
                      end_kb(rid, mines_board(data, pos)))
        elif data["step"] >= 25 - m:
            pay = int(Decimal(r["stake"]) * mines_mult(r["coeff"], m, data["step"]))
            set_round_data(rid, data)
            net = finish_round(rid, "WON", pay, "all_safe") or 0
            await out(c.message, True, f"🏆 <b>ВСЕ КЛЕТКИ ОТКРЫТЫ!</b>\n{SEP}\nВыплата: <b>{money(net)}</b>",
                      end_kb(rid, mines_board(data)))
        else:
            set_round_data(rid, data)
            text, markup = render_round(get_round(rid))
            await out(c.message, True, text, markup)
        await c.answer()


@dp.callback_query(F.data.startswith("tl:"))
async def cb_tower(c: CallbackQuery):
    try:
        _, rid, cs = c.data.split(":")
        choice = int(cs)
    except ValueError:
        return await c.answer()

    async with round_locks[rid]:
        r = get_round(rid)
        if not valid_round(r, c.from_user.id) or r["game"] != "tower":
            return await c.answer("Раунд недоступен.", show_alert=True)
        data = json.loads(r["data_json"])
        if not (0 <= choice < data["cells"]):
            return await c.answer()
        f = data["floor"]
        trap = data["danger"][f]

        if choice == trap:
            finish_round(rid, "LOST", 0, f"trap_floor {f + 1}")
            row = [("🟥" if i == trap else "🟩", "noop") for i in range(data["cells"])]
            await out(c.message, True,
                      f"🟥 <b>ЛОВУШКА!</b>\n{SEP}\nЭтаж: {f + 1}\nПотеряно: <b>{money(r['stake'])}</b>",
                      end_kb(rid, [row]))
        else:
            data["floor"] = f + 1
            if data["floor"] >= data["floors"]:
                pay = int(Decimal(r["stake"]) * tower_mult(r["coeff"], data["cells"], data["floor"]))
                set_round_data(rid, data)
                net = finish_round(rid, "WON", pay, "tower_top") or 0
                await out(c.message, True, f"🏆🎉 <b>ВЕРШИНА ПОКОРЕНА!</b> 🎉🏆\n{SEP}\nВыплата: <b>{money(net)}</b>",
                          end_kb(rid))
            else:
                set_round_data(rid, data)
                text, markup = render_round(get_round(rid))
                await out(c.message, True, text, markup)
        await c.answer()


@dp.callback_query(F.data.startswith("cash:"))
async def cb_cash(c: CallbackQuery):
    rid = c.data.split(":")[1]
    async with round_locks[rid]:
        r = get_round(rid)
        if not valid_round(r, c.from_user.id) or r["game"] not in ("mines", "tower"):
            return await c.answer("Нельзя забрать выигрыш.", show_alert=True)
        step, mult = round_progress(r)
        if step < 1:
            return await c.answer("Сделайте хотя бы один шаг.", show_alert=True)
        data = json.loads(r["data_json"])
        net = finish_round(rid, "CASHED_OUT", int(Decimal(r["stake"]) * mult), "cashout") or 0
        board = mines_board(data) if r["game"] == "mines" else None
        await out(c.message, True,
                  f"⚡ <b>ВЫИГРЫШ ЗАБРАН!</b>\n{SEP}\n📈 Множитель: <b>x{mult:.2f}</b>\n💰 Выплата: <b>{money(net)}</b>",
                  end_kb(rid, board))
        await c.answer()


@dp.callback_query(F.data.startswith("fair:"))
async def cb_fair(c: CallbackQuery):
    r = get_round(c.data.split(":")[1])
    if not r or r["user_id"] != c.from_user.id or not r["server_seed_hash"]:
        return await c.answer("Для этой игры seed недоступен.", show_alert=True)
    if r["state"] == "ACTIVE":
        return await c.answer(f"SHA-256 server_seed:\n{r['server_seed_hash']}", show_alert=True)
    data = json.loads(r["data_json"])
    if r["game"] == "mines":
        layout = f"Мины: {sorted(data['mines'])}"
    elif r["game"] == "tower":
        layout = f"Ловушки по этажам: {data['danger']}"
    else:
        layout = f"Результат: {'орёл' if data.get('result') == 'h' else 'решка'}"
    await c.message.answer(
        f"🔍 <b>ПРОВЕРКА ЧЕСТНОСТИ</b>\n{SEP}\n"
        f"server_seed:\n<code>{r['server_seed']}</code>\nSHA-256:\n<code>{r['server_seed_hash']}</code>\n"
        f"client_seed: <code>{esc(r['client_seed'])}</code>\nnonce (round_id): <code>{r['round_id']}</code>\n\n"
        f"{layout}\n\nАлгоритм: /fair"
    )
    await c.answer()


# ===================== COIN =====================

async def play_coin(t: Message, uid, side, stake):
    rid = secrets.token_hex(8)
    ss = secrets.token_hex(32)
    cs = get_client_seed(uid)
    coeff = outcome_coeff("coin", side)
    start_round(rid, uid, "coin", stake, coeff, {"param": side}, ss, cs)
    res = "h" if next(_stream(ss, cs, rid)) % 2 == 0 else "t"
    won = res == side
    set_round_data(rid, {"param": side, "result": res})
    net = finish_round(rid, "WON" if won else "LOST", int(Decimal(stake) * coeff) if won else 0, f"coin:{res}") or 0
    u = get_user(uid)
    kbd = kb([
        [("🔄 Играть снова", f"again:{rid}"), ("🔍 Проверить честность", f"fair:{rid}")],
        [("📊 Статистика", "dst:coin")],
    ])
    if won:
        await t.answer(
            f"🎉 <b>ПОБЕДА!</b>\n{SEP}\nВыпало: <b>{COIN_LABEL[res]}</b> (x{coeff:.2f})\n💰 Выплата: <b>{money(net)}</b>\n💼 Баланс: <b>{money(avail_of(u))}</b>",
            reply_markup=kbd)
    else:
        await t.answer(
            f"😢 <b>Поражение</b>\n{SEP}\nВыпало: <b>{COIN_LABEL[res]}</b>\n💸 Ставка: <b>{money(stake)}</b>\n💼 Баланс: <b>{money(avail_of(u))}</b>",
            reply_markup=kbd)


# ===================== DICE GAMES =====================

def dice_outcome(game, key, val, stake, coeff):
    """(won, payout_gross)"""
    if game == "slots":
        k = SLOT_VAL.get(val)
        if k:
            return True, int(Decimal(stake) * outcome_coeff("slots", k))
        return False, 0
    if val in outcome_values(game, key):
        return True, int(Decimal(stake) * coeff)
    return False, 0


def dice_shown(game, val):
    if game == "slots":
        k = SLOT_VAL.get(val)
        return SLOT_LABEL[k] if k else "—"
    return str(val)


async def play_dice(t: Message, uid, game, param, stake):
    pr = parse_dice_param(param)
    if not pr:
        raise GameError("Некорректные параметры игры")
    key, n = pr
    title = gtitle(game)
    if game == "slots":
        coeff = Decimal(0)
        pick = ""
    else:
        if key not in OUTCOMES[game][1]:
            raise GameError("Некорректный исход")
        coeff = outcome_coeff(game, key)
        pick = f"Исход: <b>{outcome_label(game, key)}</b> · x{coeff:.2f}\n"
    busy_users.add(uid)
    try:
        results, total, summary, last_rid = [], 0, None, None
        for i in range(n):
            rid = secrets.token_hex(8)
            try:
                start_round(rid, uid, game, stake, coeff, {"param": param})
            except GameError as e:
                await t.answer("⚠️ Недостаточно средств для продолжения." if "средств" in str(e) else f"⚠️ {e}")
                break
            last_rid = rid
            try:
                d = await bot.send_dice(t.chat.id, emoji=DICE[game])
            except Exception:
                refund_round(rid, "send_dice_failed")
                raise
            val = d.dice.value
            await asyncio.sleep(DICE_WAIT.get(game, 3.5))
            won, gross = dice_outcome(game, key, val, stake, coeff)
            net = finish_round(rid, "WON" if won else "LOST", gross, f"dice:{val}") or 0
            total += net
            results.append((val, won, net))

            if n > 1:
                lines = "\n".join(
                    f"{'🟢' if w else '🔴'} #{k + 1}: <b>{dice_shown(game, v)}</b> → {usdt(pp)}"
                    for k, (v, w, pp) in enumerate(results)
                )
                txt = f"{title} · серия {len(results)}/{n}\n{SEP}\n{pick}{lines}"
                if summary is None:
                    summary = await t.answer(txt)
                else:
                    try:
                        await summary.edit_text(txt)
                    except TelegramBadRequest:
                        pass

        if not results:
            return
        u = get_user(uid)
        avail = avail_of(u)
        again = kb([[("🔄 Играть снова", f"again:{last_rid}"), ("📊 Статистика", f"dst:{game}")],
                    [("🎮 Игры", "menu:games")]])
        if n == 1:
            val, won, net = results[0]
            shown = dice_shown(game, val)
            if won:
                await t.answer(f"🎉🎊 <b>ПОБЕДА!</b> 🎊🎉\n{SEP}\n{pick}🎯 Значение: <b>{shown}</b>\n"
                               f"💰 Выплата: <b>{money(net)}</b>\n💼 Баланс: <b>{money(avail)}</b>", reply_markup=again)
            else:
                await t.answer(f"😢 <b>Поражение</b>\n{SEP}\n{pick}🎯 Значение: <b>{shown}</b>\n"
                               f"💸 Ставка: <b>{money(stake)}</b>\n💼 Баланс: <b>{money(avail)}</b>", reply_markup=again)
        else:
            spent = stake * len(results)
            await t.answer(
                f"🏁 <b>Серия завершена</b>\n{SEP}\n{pick}💸 Поставлено: <b>{money(spent)}</b>\n"
                f"💰 Выплачено: <b>{money(total)}</b>\n"
                f"📊 Итог: <b>{'+' if total >= spent else '-'}{money(abs(total - spent))}</b>\n"
                f"💼 Баланс: <b>{money(avail)}</b>", reply_markup=again)
    finally:
        busy_users.discard(uid)


# ===================== ADMIN: HELPERS =====================

PENDING: dict = {}


def pend(admin_id, kind, **kw):
    now_t = time.time()
    for k in [k for k, v in PENDING.items() if v["exp"] < now_t]:
        PENDING.pop(k, None)
    tok = secrets.token_hex(4)
    PENDING[tok] = {"admin": admin_id, "kind": kind, "exp": now_t + 300, **kw}
    return tok


def confirm_kb(tok):
    return kb([[("✅ Подтвердить", f"adm:cf:{tok}"), ("❌ Отмена", f"adm:cx:{tok}")]])


def adm_menu_kb(owner=False):
    rows = [
        [("📊 Дашборд", "adm:dash"), ("👥 Игроки", "adm:users")],
        [("🎮 Игры / RTP", "adm:games"), ("⚙️ Настройки", "adm:set")],
        [("💳 Финансы", "adm:fin"), ("🎟 Промокоды", "adm:promos")],
        [("📣 Рассылка", "adm:bc"), ("📜 Журнал", "adm:log:0")],
        [("📤 Экспорт / бэкап", "adm:exp"), ("🩺 Система", "adm:sys")],
    ]
    if owner:
        rows.append([("🛡 Админы", "adm:admins")])
    rows.append([("✖️ Закрыть", "adm:close")])
    return kb(rows)


def back_kb(to="adm:home", text="⬅️ Админ-меню"):
    return kb([[(text, to)]])


ADM_TITLE = "👑 <b>Napoleon Admin Panel</b>"


def adm_home_text():
    users = q1("SELECT COUNT(*) n FROM users")["n"]
    rev = q1("SELECT COALESCE(SUM(stake)-SUM(payout),0) v FROM rounds WHERE state!='ACTIVE'")["v"]
    rw = q1("SELECT COUNT(*) n FROM withdrawals WHERE status IN('REVIEW','UNKNOWN')")["n"]
    mt = "🔴 ВКЛЮЧЕНЫ ТЕХРАБОТЫ\n" if Sb("maintenance") else ""
    return (f"{ADM_TITLE}\n{SEP}\n{mt}👥 Игроков: <b>{users}</b>\n🏦 GGR: <b>{money(rev)}</b>\n"
            f"🕵️ Заявок требуют внимания: <b>{rw}</b>")


# ===================== ADMIN: ENTRY =====================

@adm.message(F.text == "👑 Админ")
@adm.message(Command("admin"))
async def nav_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(adm_home_text(), reply_markup=adm_menu_kb(is_owner(m.from_user.id)))


@deny.callback_query(F.data.startswith("adm:"))
async def cb_adm_deny(c: CallbackQuery):
    await c.answer("⛔ Доступ запрещён.", show_alert=True)


# ===================== ADMIN: DASHBOARD / SYSTEM =====================

def dash_text():
    d1, d7 = since(days=1), since(days=7)
    day0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    def s(sql, a=()):
        return q1(sql, a)["v"] or 0

    users = s("SELECT COUNT(*) v FROM users")
    new_t = s("SELECT COUNT(*) v FROM users WHERE created_at >= ?", (day0,))
    a24 = s("SELECT COUNT(*) v FROM users WHERE last_seen >= ?", (d1,))
    a7 = s("SELECT COUNT(*) v FROM users WHERE last_seen >= ?", (d7,))
    r_today = s("SELECT COUNT(*) v FROM rounds WHERE started_at >= ?", (day0,))
    r7 = s("SELECT COUNT(*) v FROM rounds WHERE started_at >= ?", (d7,))
    total_bal = s("SELECT SUM(balance) v FROM balances")
    dep_t = s("SELECT SUM(amount_cents) v FROM invoices WHERE status='PAID' AND paid_at >= ?", (day0,))
    dep7 = s("SELECT SUM(amount_cents) v FROM invoices WHERE status='PAID' AND paid_at >= ?", (d7,))
    wd7 = s("SELECT SUM(amount_cents) v FROM withdrawals WHERE status='DONE' AND processed_at >= ?", (d7,))
    ggr = s("SELECT SUM(stake)-SUM(payout) v FROM rounds WHERE state!='ACTIVE'")
    ggr7 = s("SELECT SUM(stake)-SUM(payout) v FROM rounds WHERE state!='ACTIVE' AND started_at >= ?", (d7,))
    turn7 = s("SELECT SUM(stake) v FROM rounds WHERE started_at >= ?", (d7,))
    top = qa("SELECT game, COUNT(*) n FROM rounds GROUP BY game ORDER BY n DESC LIMIT 4")
    tops = "\n".join(
        f"  {i + 1}. {gtitle(r['game']) if r['game'] in GAMES else r['game']} — {r['n']}" for i, r in enumerate(top)
    ) or "  —"
    return (
        f"📊 <b>ДАШБОРД</b>\n{SEP}\n"
        f"👥 Игроков: <b>{users}</b> · новых сегодня <b>{new_t}</b>\n"
        f"🟢 Активны: 24ч <b>{a24}</b> · 7д <b>{a7}</b>\n"
        f"🎲 Раундов: сегодня <b>{r_today}</b> · 7д <b>{r7}</b>\n"
        f"💰 Сумма балансов (долг): <b>{money(total_bal)}</b>\n"
        f"📥 Депозиты: сегодня <b>{money(dep_t)}</b> · 7д <b>{money(dep7)}</b>\n"
        f"📤 Выводы 7д: <b>{money(wd7)}</b>\n"
        f"🔁 Оборот 7д: <b>{money(turn7)}</b>\n"
        f"🏦 GGR: всего <b>{money(ggr)}</b> · 7д <b>{money(ggr7)}</b>\n\n🔥 Топ игр:\n{tops}"
    )


def uptime():
    s = int(time.time() - STARTED_AT)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    return f"{d}д {h}ч {r // 60}м"


HB: dict = {}


def integrity_report():
    with db() as c:
        ic = c.execute("PRAGMA integrity_check").fetchone()[0]
        bad = [dict(r) for r in c.execute(
            """SELECT b.telegram_id, b.balance, COALESCE(SUM(l.delta),0) s
               FROM balances b LEFT JOIN ledger l ON l.telegram_id=b.telegram_id
               GROUP BY b.telegram_id HAVING b.balance != COALESCE(SUM(l.delta),0) LIMIT 10""")]
        neg = c.execute("SELECT COUNT(*) FROM balances WHERE reserved_balance > balance").fetchone()[0]
    return ic, bad, neg


def sys_text():
    ic, bad, neg = integrity_report()
    size = os.path.getsize(DB_PATH) / 1024 / 1024 if os.path.exists(DB_PATH) else 0
    t = time.time()
    hb = "\n".join(
        f"{'🟢' if t - v < 120 else '🔴'} {k}: {int(t - v)}с назад" for k, v in sorted(HB.items())
    ) or "—"
    act = q1("SELECT COUNT(*) n FROM rounds WHERE state='ACTIVE'")["n"]
    pi = q1("SELECT COUNT(*) n FROM invoices WHERE status='PENDING'")["n"]
    pw = q1("SELECT COUNT(*) n FROM withdrawals WHERE status IN('PENDING','PROCESSING')")["n"]
    badtxt = "✅ расхождений нет" if not bad else "⚠️ расхождения (ID: баланс/леджер):\n" + "\n".join(
        f"  {b['telegram_id']}: {usdt(b['balance'])}/{usdt(b['s'])}" for b in bad)
    return (
        f"🩺 <b>СИСТЕМА</b>\n{SEP}\n"
        f"⏱ Аптайм: <b>{uptime()}</b>\n🐍 Python {platform.python_version()} · aiogram {aiogram.__version__} · httpx {httpx.__version__}\n"
        f"🗄 БД: <b>{size:.2f} МБ</b> · integrity: <b>{esc(ic)}</b> · схема v{S('schema_version')}\n"
        f"🧮 Баланс vs леджер: {badtxt}\n"
        f"🔒 Резерв > баланс: <b>{neg}</b>\n"
        f"🎲 Активных раундов: <b>{act}</b> · счетов в ожидании: <b>{pi}</b> · выплат в работе: <b>{pw}</b>\n\n"
        f"⚙️ Фоновые задачи:\n{hb}"
    )


# ===================== ADMIN: CALLBACKS =====================

def games_menu_kb():
    rows = [[(f"{'🟢' if gs(g)['enabled'] else '🔴'} {gtitle(g)} · {displayed_rtp(g)}", f"adm:g:{g}")] for g in GAMES]
    rows.append([("📊 Задать RTP всем играм", "adm:rtpall")])
    rows.append([("⬅️ Админ-меню", "adm:home")])
    return kb(rows)


DICE_LIKE = set(DICE_MAX)
HAS_OUTCOMES = set(OUTCOMES) | {"coin", "slots"}


def game_stats_line(g):
    r = q1(
        "SELECT COUNT(*) n, COALESCE(SUM(stake),0) st, COALESCE(SUM(payout),0) pay FROM rounds "
        "WHERE game=? AND state IN ('WON','LOST','CASHED_OUT')", (g,))
    r7 = q1(
        "SELECT COUNT(*) n, COALESCE(SUM(stake),0) st, COALESCE(SUM(payout),0) pay FROM rounds "
        "WHERE game=? AND state IN ('WON','LOST','CASHED_OUT') AND started_at >= ?", (g, since(days=7)))

    def fmt(x):
        if not x["n"] or not x["st"]:
            return "нет данных"
        return f"{x['n']} раундов · оборот {usdt(x['st'])} · выплаты {usdt(x['pay'])} · <b>RTP {x['pay'] / x['st'] * 100:.1f}%</b>"

    return f"📈 Фактически всего: {fmt(r)}\n📈 Фактически 7 дней: {fmt(r7)}"


def game_card(g):
    s = gs(g)
    lo, hi = stake_bounds(g)
    lines = [
        f"<b>{gtitle(g)}</b> · {'🟢 включена' if s['enabled'] else '🔴 выключена'}",
        SEP,
        f"🎯 Заданный RTP: <b>{s['rtp']}%</b> · по реальным коэффициентам: <b>{displayed_rtp(g)}</b>",
        f"👁 Игрокам RTP: <b>{'показывается' if Sb('show_rtp') else 'скрыт'}</b> (настройка «Показывать RTP игрокам»)",
        f"💵 Ставки: <b>{usdt(lo)}</b> – <b>{usdt(hi)}</b> USDT"
        + (" (общие)" if not s["min_stake"] and not s["max_stake"] else ""),
    ]
    if g == "mines":
        lines.append(f"🧮 Пример: 3 мины, 1 шаг → x{mines_mult(game_rtp(g), 3, 1):.2f}, 5 шагов → x{mines_mult(game_rtp(g), 3, 5):.2f}")
    elif g == "tower":
        lines.append(f"🧮 Пример: «Стандарт», 3 этажа → x{tower_mult(game_rtp(g), 3, 3):.2f}, 8 этажей → x{tower_mult(game_rtp(g), 3, 8):.2f}")
    elif g in HAS_OUTCOMES:
        keys = outcome_keys(g)
        lines.append(f"🎲 Исходов: <b>{len(keys)}</b> (включено {len(enabled_outcomes(g))}). Коэффициент = RTP ÷ вероятность исхода; "
                     f"для каждого можно задать свой в «🎯 Исходы».")
    lines.append(game_stats_line(g))
    lines.append("\n<i>Множители Мин/Башни и коэффициенты исходов считаются честно от RTP — шанс мины, ловушки или результата "
                 "кубика не подкручивается. Для игр на эмодзи Telegram результат определяет Telegram.</i>")
    rows = [
        [(("🔴 Выключить" if s["enabled"] else "🟢 Включить"), f"adm:gt:{g}"), ("✏️ Название", f"adm:gf:{g}:title")],
        [("📊 Изменить RTP", f"adm:gf:{g}:rtp")],
        [("⬇️ Мин. ставка", f"adm:gf:{g}:min_stake"), ("⬆️ Макс. ставка", f"adm:gf:{g}:max_stake")],
    ]
    if g in HAS_OUTCOMES:
        rows.append([("🎯 Исходы / коэффициенты", f"adm:go:{g}")])
    rows.append([("♻️ Сброс до стандарта", f"adm:grs:{g}")])
    rows.append([("⬅️ Игры", "adm:games")])
    return "\n".join(lines), kb(rows)


def outcomes_view(g):
    lines = [f"🎯 <b>{gtitle(g)} — исходы</b>", SEP,
             f"Заданный RTP: <b>{gs(g)['rtp']}%</b> · по факту: <b>{displayed_rtp(g)}</b>", ""]
    rows = []
    for k in outcome_keys(g):
        on = outcome_enabled(g, k)
        ov = outcome_override(g, k)
        pr = outcome_prob(g, k) * 100
        lines.append(
            f"{'🟢' if on else '🔴'} {outcome_label(g, k)} — шанс {pr:.1f}% · <b>x{outcome_coeff(g, k):.2f}</b>"
            f"{' 🔧' if ov > 0 else ''} · RTP {outcome_rtp(g, k) * 100:.1f}%"
        )
        rows.append([(f"⚙️ {outcome_label(g, k)}", f"adm:gk:{g}:{k}")])
    lines.append("\n🔧 — коэффициент задан вручную. Коэффициент выше RTP÷шанс делает исход невыгодным для казино.")
    rows.append([("⬅️ К игре", f"adm:g:{g}")])
    return "\n".join(lines), kb(rows)


def outcome_card(g, k):
    on = outcome_enabled(g, k)
    ov = outcome_override(g, k)
    vals = outcome_values(g, k)
    lines = [
        f"⚙️ <b>{gtitle(g)} → {outcome_label(g, k)}</b>", SEP,
        f"Статус: <b>{'🟢 включён' if on else '🔴 выключен'}</b>",
        (f"Выигрышные значения: <b>{', '.join(map(str, vals))}</b>" if vals else "Выигрыш определяется результатом игры"),
        f"Шанс: <b>{outcome_prob(g, k) * 100:.2f}%</b>",
        f"Авто-коэффициент (по RTP {gs(g)['rtp']}%): <b>x{outcome_auto_coeff(g, k):.2f}</b>",
        f"Действующий коэффициент: <b>x{outcome_coeff(g, k):.2f}</b>" + (" 🔧 (ручной)" if ov > 0 else " (авто)"),
        f"RTP исхода: <b>{outcome_rtp(g, k) * 100:.2f}%</b>",
    ]
    rows = []
    if g != "slots":
        rows.append([(("🔴 Выключить" if on else "🟢 Включить"), f"adm:gkt:{g}:{k}")])
    rows.append([("✖️ Задать коэффициент", f"adm:gkc:{g}:{k}"), ("♻️ Авто по RTP", f"adm:gkr:{g}:{k}")])
    rows.append([("⬅️ Исходы", f"adm:go:{g}")])
    return "\n".join(lines), kb(rows)


def promo_card(code):
    r = q1("SELECT * FROM promos WHERE code=?", (code,))
    if not r:
        return None, None
    return (f"🎟 <b>{esc(code)}</b>\nСумма: {money(r['amount'])}\nАктиваций: {r['used']}/{r['max_uses']}\n"
            f"Статус: {'активен' if r['active'] else 'выключен'}",
            kb([[("🔴 Выключить" if r["active"] else "🟢 Включить", f"adm:prt:{code}"), ("🗑 Удалить", f"adm:prd:{code}")],
                [("⬅️ Промокоды", "adm:promos")]]))


def settings_group_view(g):
    rows = [[(f"{SETTINGS[k][1]}: {show_val(k)}", f"adm:se:{k}")] for k in GROUP_KEYS[g]]
    rows.append([("⬅️ Настройки", "adm:set")])
    return f"{GROUPS[g]}\n{SEP}\nНажмите на параметр, чтобы изменить.", kb(rows)


def user_card(uid):
    u = get_user(uid)
    if not u:
        return None, None
    dep = deposits_total(uid)
    wd = q1("SELECT COALESCE(SUM(amount_cents),0) s FROM withdrawals WHERE user_id=? AND status='DONE'", (uid,))["s"]
    nref = q1("SELECT COUNT(*) n FROM users WHERE referrer_id=?", (uid,))["n"]
    need, have = wager_state(uid)
    uname = f"@{esc(u['username'])}" if u["username"] else "—"
    text = (
        f"👤 <b>Игрок</b> <code>{uid}</code>\n{SEP}\n"
        f"Имя: <b>{esc(u['name'])}</b> · {uname}\n"
        f"Статус: <b>{u['status']}</b> · {esc(u['vip_level'])}\n"
        f"💰 Баланс: <b>{money(u['balance'])}</b> (в выводе {money(u['reserved_balance'])})\n"
        f"📥 Депозиты: <b>{money(dep)}</b> · 📤 Выводы: <b>{money(wd)}</b>\n"
        f"📊 Оборот: <b>{money(u['total_wagered'])}</b> / нужно <b>{money(need)}</b>\n"
        f"🎲 Раундов: <b>{count_rounds(uid)}</b> · рефералов: <b>{nref}</b>\n"
        f"👥 Пригласил: <code>{u['referrer_id'] or '—'}</code>\n"
        f"💸 Вывод: <b>{'🚫 заблокирован' if u['wd_blocked'] else 'разрешён'}</b>\n"
        f"🎰 Лимит ставки: <b>{usdt(u['max_stake']) + ' USDT' if u['max_stake'] else 'нет'}</b>\n"
        f"📝 Заметка: {esc(u['note']) or '—'}\n"
        f"🕐 Регистрация: {u['created_at'][:16].replace('T', ' ')} · был: {u['last_seen'][:16].replace('T', ' ')}"
    )
    banned = u["status"] == "BANNED"
    rows = [
        [("➕ Баланс", f"adm:ub:{uid}:+"), ("➖ Баланс", f"adm:ub:{uid}:-"), ("🎯 Задать", f"adm:ub:{uid}:=")],
        [("✅ Разбанить" if banned else "🚫 Забанить", f"adm:uban:{uid}"),
         ("💸 Разрешить вывод" if u["wd_blocked"] else "💸 Блок вывода", f"adm:uwd:{uid}")],
        [("🎰 Лимит ставки", f"adm:uf:{uid}:max_stake"), ("📝 Заметка", f"adm:uf:{uid}:note")],
        [("✉️ Написать", f"adm:um:{uid}"), ("🧾 Леджер", f"adm:ul:{uid}"), ("🎲 Раунды", f"adm:uh:{uid}")],
        [("🔄 Обновить", f"adm:u:{uid}"), ("⬅️ Игроки", "adm:users")],
    ]
    return text, kb(rows)


async def run_pending(p, admin_id):
    k = p["kind"]
    if k == "bal":
        uid = p["uid"]
        before, after = change_balance(uid, p.get("delta", 0), p["reason"], admin_id, set_to=p.get("set_to"))
        await notify_user(uid, f"🎩 Администратор изменил ваш баланс: <b>{usdt(before)} → {usdt(after)} USDT</b>.\nПричина: {esc(p['reason'])}")
        return f"✅ Баланс {uid}: {money(before)} → {money(after)}"
    if k == "ban":
        ex("UPDATE users SET status=? WHERE telegram_id=?", ("BANNED" if p["ban"] else "ACTIVE", p["uid"]))
        audit(admin_id, "ban" if p["ban"] else "unban", p["uid"])
        return f"✅ Игрок {p['uid']} {'заблокирован' if p['ban'] else 'разблокирован'}"
    if k == "bcast":
        t = asyncio.create_task(do_broadcast(admin_id, p["text"]))
        BG_TASKS.add(t)
        t.add_done_callback(BG_TASKS.discard)
        return "📣 Рассылка запущена. Отчёт придёт по завершении."
    if k == "promo_del":
        ex("DELETE FROM promos WHERE code=?", (p["code"],))
        audit(admin_id, "promo_del", p["code"])
        return f"🗑 Промокод {p['code']} удалён"
    if k == "admin_rm":
        ex("DELETE FROM admins WHERE telegram_id=?", (p["uid"],))
        load_admins()
        audit(admin_id, "admin_rm", p["uid"])
        return f"🗑 Админ {p['uid']} удалён"
    if k == "game_reset":
        g = p["game"]
        rtp, wmin, title = GAME_DEFAULTS[g]
        ex("UPDATE game_settings SET enabled=1, title=?, min_stake=0, max_stake=0, win_min=?, rtp=?, coeff_override='0' WHERE game=?",
           (title, wmin, rtp, g))
        ex("DELETE FROM outcome_settings WHERE game=?", (g,))
        audit(admin_id, "game_reset", g)
        return f"♻️ {g} сброшена"
    if k == "wd_fail":
        w = finalize_withdrawal(p["wid"], False, admin_id, from_states=("PENDING", "REVIEW", "UNKNOWN"))
        if not w:
            return "⚠️ Заявка уже обработана"
        audit(admin_id, "wd_reject", p["wid"])
        await notify_user(w["user_id"], f"❌ Заявка №{w['id']} отклонена. Средства возвращены на баланс.")
        return f"❌ Заявка #{p['wid']} отклонена, резерв снят"
    if k == "wd_done":
        w = finalize_withdrawal(p["wid"], True, admin_id, "manual", from_states=("PENDING", "REVIEW", "UNKNOWN"))
        if not w:
            return "⚠️ Заявка уже обработана"
        audit(admin_id, "wd_manual_done", p["wid"])
        await notify_user(w["user_id"], f"✅ Заявка №{w['id']} выполнена. Сумма: <b>{money(w['amount_cents'])}</b>")
        return f"✅ Заявка #{p['wid']} отмечена выплаченной (баланс списан)"
    if k == "setting_reset":
        key = p["key"]
        set_setting(key, SETTINGS[key][3])
        audit(admin_id, "setting_reset", key)
        return f"♻️ {key} сброшен"
    return "⚠️ Неизвестное действие"


@adm.callback_query(F.data.startswith("adm:"))
async def cb_admin(c: CallbackQuery, state: FSMContext):
    admin_id = c.from_user.id
    p = c.data.split(":")
    a = p[1]
    m = c.message

    try:
        if a not in ("cf", "cx"):
            await state.clear()

        if a == "home":
            await out(m, True, adm_home_text(), adm_menu_kb(is_owner(admin_id)))

        elif a == "close":
            try:
                await m.delete()
            except Exception:
                pass

        elif a == "dash":
            await out(m, True, dash_text(), kb([[("🔄 Обновить", "adm:dash")], [("⬅️ Админ-меню", "adm:home")]]))

        elif a == "sys":
            await out(m, True, sys_text(), kb([[("🔄 Обновить", "adm:sys")], [("⬅️ Админ-меню", "adm:home")]]))

        # ---------- settings ----------
        elif a == "set":
            rows = chunk([(GROUPS[g], f"adm:sg:{g}") for g in GROUPS], 2)
            rows.append([("⬅️ Админ-меню", "adm:home")])
            await out(m, True, f"⚙️ <b>НАСТРОЙКИ</b>\n{SEP}\nВыберите раздел:", kb(rows))

        elif a == "sg":
            g = p[2]
            if g not in GROUPS:
                return await c.answer()
            t, k_ = settings_group_view(g)
            await out(m, True, t, k_)

        elif a == "se":
            key = p[2]
            if key not in SETTINGS:
                return await c.answer()
            grp, label, kind, default, hint = SETTINGS[key]
            if kind == "bool":
                set_setting(key, "0" if S(key) == "1" else "1")
                audit(admin_id, "edit_setting", f"{key}: -> {S(key)}")
                t, k_ = settings_group_view(grp)
                await out(m, True, t, k_)
            else:
                await state.set_state(Adm.setting_value)
                await state.update_data(key=key)
                cur = mask(S(key)) if kind == "secret" else (S(key) or "—")
                opts = f"\nДопустимо: {kind.split(':', 1)[1]}" if kind.startswith("choice:") else ""
                await out(
                    m, True,
                    f"✏️ <b>{label}</b>\n{SEP}\nТекущее: <code>{esc(cur)[:300]}</code>\nПо умолчанию: <code>{esc(default or '—')}</code>\n"
                    f"{esc(hint)}{opts}\n\nОтправьте новое значение (или «-» чтобы очистить).",
                    kb([[("♻️ Сбросить по умолчанию", f"adm:sr:{key}")], [("⬅️ Отмена", f"adm:sg:{grp}")]]),
                )

        elif a == "sr":
            key = p[2]
            if key not in SETTINGS:
                return await c.answer()
            tok = pend(admin_id, "setting_reset", key=key)
            await out(m, True, f"♻️ Сбросить <b>{SETTINGS[key][1]}</b> на «{esc(SETTINGS[key][3] or '—')}»?", confirm_kb(tok))

        # ---------- games ----------
        elif a == "games":
            await out(m, True, f"🎮 <b>УПРАВЛЕНИЕ ИГРАМИ</b>\n{SEP}\nВыберите игру:", games_menu_kb())

        elif a == "g":
            g = p[2]
            if g not in GAMES:
                return await c.answer()
            t, k_ = game_card(g)
            await out(m, True, t, k_)

        elif a == "gt":
            g = p[2]
            if g not in GAMES:
                return await c.answer()
            ex("UPDATE game_settings SET enabled=1-enabled WHERE game=?", (g,))
            audit(admin_id, "toggle_game", g)
            t, k_ = game_card(g)
            await out(m, True, t, k_)

        elif a == "gf":
            g, field = p[2], p[3]
            if g not in GAMES or field not in ("title", "rtp", "min_stake", "max_stake"):
                return await c.answer()
            await state.set_state(Adm.game_value)
            await state.update_data(game=g, field=field)
            hints = {
                "title": "Новое название (без эмодзи)",
                "rtp": "RTP в процентах, от 1 до 100 (например 95). Коэффициенты и множители пересчитаются автоматически, игроки увидят новый RTP. Коэффициент не опускается ниже x1.00, поэтому при очень низком RTP фактический RTP исхода может быть выше заданного — он честно показан в карточке",
                "min_stake": "Мин. ставка в USDT (0 — общая)",
                "max_stake": "Макс. ставка в USDT (0 — общая)",
                "win_min": f"Порог победы, 2–{DICE_MAX.get(g, 6)}",
                "coeff_override": "Коэффициент (например 1.9). 0 — авто по RTP",
            }
            await out(m, True, f"✏️ <b>{gtitle(g)}</b> · {field}\n{SEP}\n{hints[field]}\n\nОтправьте значение:",
                      kb([[("⬅️ Отмена", f"adm:g:{g}")]]))

        elif a == "grs":
            g = p[2]
            if g not in GAME_DEFAULTS:
                return await c.answer()
            tok = pend(admin_id, "game_reset", game=g)
            await out(m, True, f"♻️ Сбросить настройки <b>{gtitle(g)}</b> до стандартных?", confirm_kb(tok))

        elif a == "rtpall":
            await state.set_state(Adm.game_value)
            await state.update_data(game="*", field="rtp")
            await out(m, True, f"📊 <b>RTP для ВСЕХ игр</b>\n{SEP}\nОтправьте RTP в процентах (1–100), например <code>95</code>.\n"
                      "Ручные коэффициенты исходов сохранятся.", back_kb("adm:games", "⬅️ Отмена"))

        elif a == "go":
            g = p[2]
            if g not in HAS_OUTCOMES:
                return await c.answer()
            t, k_ = outcomes_view(g)
            await out(m, True, t, k_)

        elif a == "gk":
            g, k = p[2], p[3]
            if g not in HAS_OUTCOMES or k not in outcome_keys(g):
                return await c.answer()
            t, k_ = outcome_card(g, k)
            await out(m, True, t, k_)

        elif a == "gkt":
            g, k = p[2], p[3]
            if g not in HAS_OUTCOMES or g == "slots" or k not in outcome_keys(g):
                return await c.answer()
            set_outcome(g, k, enabled=not outcome_enabled(g, k))
            audit(admin_id, "toggle_outcome", f"{g}.{k}")
            t, k_ = outcome_card(g, k)
            await out(m, True, t, k_)

        elif a == "gkc":
            g, k = p[2], p[3]
            if g not in HAS_OUTCOMES or k not in outcome_keys(g):
                return await c.answer()
            await state.set_state(Adm.game_value)
            await state.update_data(game=g, field="oc", key=k)
            await out(m, True,
                      f"✖️ <b>{gtitle(g)} → {outcome_label(g, k)}</b>\n{SEP}\n"
                      f"Авто-коэффициент по RTP: <b>x{outcome_auto_coeff(g, k):.2f}</b>\n"
                      f"Отправьте новый коэффициент (например <code>1.9</code>, максимум 1000). <code>0</code> — вернуть авто.",
                      back_kb(f"adm:gk:{g}:{k}", "⬅️ Отмена"))

        elif a == "gkr":
            g, k = p[2], p[3]
            if g not in HAS_OUTCOMES or k not in outcome_keys(g):
                return await c.answer()
            set_outcome(g, k, coeff="0")
            audit(admin_id, "outcome_coeff_reset", f"{g}.{k}")
            t, k_ = outcome_card(g, k)
            await out(m, True, t, k_)

        # ---------- users ----------
        elif a == "users":
            await out(m, True, f"👥 <b>ИГРОКИ</b>\n{SEP}",
                      kb([[("🔎 Найти по ID/@username", "adm:us")],
                          [("💰 Топ по балансу", "adm:ul10:bal"), ("🆕 Новые", "adm:ul10:new")],
                          [("🚫 Заблокированные", "adm:ul10:ban"), ("📊 Топ по обороту", "adm:ul10:wag")],
                          [("⬅️ Админ-меню", "adm:home")]]))

        elif a == "us":
            await state.set_state(Adm.user_search)
            await out(m, True, "🔎 Отправьте ID, @username или часть имени игрока:", back_kb("adm:users", "⬅️ Отмена"))

        elif a == "ul10":
            mode = p[2]
            q = {
                "bal": "SELECT u.telegram_id id, u.name, b.balance v FROM users u JOIN balances b USING(telegram_id) ORDER BY b.balance DESC LIMIT 10",
                "new": "SELECT telegram_id id, name, 0 v FROM users ORDER BY created_at DESC LIMIT 10",
                "ban": "SELECT telegram_id id, name, 0 v FROM users WHERE status='BANNED' ORDER BY last_seen DESC LIMIT 10",
                "wag": "SELECT u.telegram_id id, u.name, b.total_wagered v FROM users u JOIN balances b USING(telegram_id) ORDER BY b.total_wagered DESC LIMIT 10",
            }.get(mode)
            if not q:
                return await c.answer()
            rows = qa(q)
            btns = [[(f"{r['name'][:20]} · {r['id']}" + (f" · {usdt(r['v'])}" if r["v"] else ""), f"adm:u:{r['id']}")] for r in rows]
            btns.append([("⬅️ Игроки", "adm:users")])
            await out(m, True, f"👥 <b>Список</b>\n{SEP}" + ("" if rows else "\nПусто."), kb(btns))

        elif a == "u":
            t, k_ = user_card(int(p[2]))
            if not t:
                return await c.answer("Игрок не найден", show_alert=True)
            await out(m, True, t, k_)

        elif a == "ub":
            uid, sign = int(p[2]), p[3]
            await state.set_state(Adm.user_amount)
            await state.update_data(uid=uid, sign=sign)
            what = {"+": "пополнение", "-": "списание", "=": "установка баланса"}[sign]
            await out(m, True,
                      f"💰 Игрок <code>{uid}</code>: <b>{what}</b>\nОтправьте: <code>сумма причина</code>\nНапример: <code>5 компенсация</code>",
                      back_kb(f"adm:u:{uid}", "⬅️ Отмена"))

        elif a == "uban":
            uid = int(p[2])
            if is_admin(uid):
                return await c.answer("Нельзя блокировать администратора", show_alert=True)
            st_ = user_status(uid)
            tok = pend(admin_id, "ban", uid=uid, ban=(st_ != "BANNED"))
            await out(m, True, f"{'🚫 Заблокировать' if st_ != 'BANNED' else '✅ Разблокировать'} игрока <code>{uid}</code>?", confirm_kb(tok))

        elif a == "uwd":
            uid = int(p[2])
            ex("UPDATE users SET wd_blocked=1-wd_blocked WHERE telegram_id=?", (uid,))
            audit(admin_id, "toggle_wd_block", uid)
            t, k_ = user_card(uid)
            await out(m, True, t, k_)

        elif a == "uf":
            uid, field = int(p[2]), p[3]
            if field not in ("max_stake", "note"):
                return await c.answer()
            await state.set_state(Adm.user_field)
            await state.update_data(uid=uid, field=field)
            hint = "Макс. ставка в USDT (0 — без лимита)" if field == "max_stake" else "Текст заметки («-» очистить)"
            await out(m, True, f"✏️ Игрок <code>{uid}</code>\n{hint}:", back_kb(f"adm:u:{uid}", "⬅️ Отмена"))

        elif a == "um":
            uid = int(p[2])
            await state.set_state(Adm.user_msg)
            await state.update_data(uid=uid)
            await out(m, True, f"✉️ Сообщение игроку <code>{uid}</code> (HTML). Отправьте текст:", back_kb(f"adm:u:{uid}", "⬅️ Отмена"))

        elif a == "ul":
            uid = int(p[2])
            rows = qa("SELECT * FROM ledger WHERE telegram_id=? ORDER BY id DESC LIMIT 15", (uid,))
            body = "\n".join(
                f"{'➕' if r['delta'] > 0 else '➖'} {usdt(abs(r['delta']))} → {usdt(r['balance_after'])} · {esc(r['reason'])[:32]}"
                for r in rows) or "Пусто."
            await out(m, True, f"🧾 <b>Леджер {uid}</b>\n{SEP}\n{body}", back_kb(f"adm:u:{uid}", "⬅️ Карточка"))

        elif a == "uh":
            uid = int(p[2])
            rows = qa("SELECT * FROM rounds WHERE user_id=? ORDER BY started_at DESC LIMIT 15", (uid,))
            body = "\n".join(
                f"{r['game']} · {r['state']} · {usdt(r['stake'])} → {usdt(r['payout'])}" for r in rows) or "Пусто."
            await out(m, True, f"🎲 <b>Раунды {uid}</b>\n{SEP}\n{body}", back_kb(f"adm:u:{uid}", "⬅️ Карточка"))

        # ---------- finance ----------
        elif a == "fin":
            inv = qa("SELECT * FROM invoices WHERE status='PENDING' ORDER BY created_at DESC LIMIT 8")
            wds = qa("SELECT * FROM withdrawals WHERE status IN('PENDING','PROCESSING','UNKNOWN','REVIEW') ORDER BY id DESC LIMIT 10")
            inv_txt = "\n".join(f"• <code>{r['invoice_id']}</code> · {r['user_id']} · <b>{usdt(r['amount_cents'])}</b>" for r in inv) or "Нет."
            wd_txt = "\n".join(f"• #{r['id']} · {r['user_id']} · <b>{usdt(r['amount_cents'])}</b> · {r['provider']} · {r['status']}" for r in wds) or "Нет."
            rows = [[(f"#{r['id']} · {usdt(r['amount_cents'])} · {r['status']}", f"adm:wd:{r['id']}")] for r in wds]
            rows += [[("🔄 Проверить счета", "adm:fcheck"), ("💼 Балансы провайдеров", "adm:pb")], [("⬅️ Админ-меню", "adm:home")]]
            await out(m, True, f"💳 <b>ФИНАНСЫ</b>\n{SEP}\n📥 Ожидающие счета:\n{inv_txt}\n\n📤 Активные выводы:\n{wd_txt}", kb(rows))

        elif a == "fcheck":
            rows = qa("SELECT invoice_id FROM invoices WHERE status='PENDING' LIMIT 30")
            ok = 0
            for r in rows:
                if await process_invoice(r["invoice_id"], notify=True) == "paid":
                    ok += 1
            await c.answer(f"Проверено: {len(rows)}, зачислено: {ok}", show_alert=True)
            return

        elif a == "pb":
            lines = []
            try:
                if cb_token():
                    d = await http_json("GET", f"{CRYPTO_API}/getBalance", headers=cb_headers())
                    if d.get("ok"):
                        bal = [f"{x['currency_code']} {x['available']}" for x in d["result"] if Decimal(str(x["available"])) > 0]
                        lines.append("💎 CryptoBot: " + (", ".join(bal) or "пусто"))
                    else:
                        lines.append(f"💎 CryptoBot: ошибка {esc(cb_err(d))}")
            except Exception as e:
                lines.append(f"💎 CryptoBot: недоступен ({esc(e)})")
            await out(m, True, f"💼 <b>БАЛАНСЫ ПРОВАЙДЕРОВ</b>\n{SEP}\n" + "\n".join(lines or ["Токены не заданы"]), back_kb("adm:fin", "⬅️ Финансы"))

        elif a == "wd":
            w = q1("SELECT * FROM withdrawals WHERE id=?", (int(p[2]),))
            if not w:
                return await c.answer("Не найдено", show_alert=True)
            text = (f"💸 <b>Вывод #{w['id']}</b>\n{SEP}\nИгрок: <code>{w['user_id']}</code>\nСумма: <b>{money(w['amount_cents'])}</b>\n"
                    f"Провайдер: {w['provider']}\nСтатус: <b>{w['status']}</b>\nTX: <code>{esc(w['tx_id'] or '—')}</code>\n"
                    f"Заметка: {esc(w['admin_note'] or '—')}\nСоздана: {w['created_at'][:16].replace('T', ' ')}")
            rows = []
            if w["status"] == "REVIEW":
                rows.append([("✅ Одобрить (авто-выплата)", f"adm:wda:{w['id']}"), ("❌ Отклонить", f"adm:wdr:{w['id']}")])
            if w["status"] in ("REVIEW", "UNKNOWN", "PENDING"):
                rows.append([("✅ Выплачено вручную", f"adm:wdd:{w['id']}")])
            if w["status"] in ("UNKNOWN", "PENDING"):
                rows.append([("↩️ Вернуть игроку (отклонить)", f"adm:wdr:{w['id']}")])
            rows.append([("👤 Игрок", f"adm:u:{w['user_id']}"), ("⬅️ Финансы", "adm:fin")])
            await out(m, True, text, kb(rows))

        elif a == "wda":
            wid = int(p[2])
            with tx() as cx:
                n = cx.execute("UPDATE withdrawals SET status='PENDING', admin_note=? WHERE id=? AND status='REVIEW'",
                               (f"одобрено {admin_id}", wid)).rowcount
            if n:
                audit(admin_id, "wd_approve", wid)
                await out(m, True, f"✅ Заявка #{wid} одобрена — выплата уйдёт автоматически в течение минуты.", back_kb("adm:fin", "⬅️ Финансы"))
            else:
                await c.answer("Заявка уже обработана", show_alert=True)

        elif a in ("wdr", "wdd"):
            wid = int(p[2])
            kind = "wd_fail" if a == "wdr" else "wd_done"
            tok = pend(admin_id, kind, wid=wid)
            what = "ОТКЛОНИТЬ и вернуть средства игроку" if a == "wdr" else "отметить как ВЫПЛАЧЕННУЮ вручную (баланс будет списан)"
            await out(m, True, f"⚠️ Заявка #{wid}: {what}?\nУбедитесь, что сверили статус у провайдера.", confirm_kb(tok))

        # ---------- promos ----------
        elif a == "promos":
            rows_ = qa("SELECT * FROM promos ORDER BY created_at DESC LIMIT 15")
            body = "\n".join(f"{'🟢' if r['active'] else '🔴'} <code>{esc(r['code'])}</code> · {usdt(r['amount'])} USDT · {r['used']}/{r['max_uses']}" for r in rows_) or "Нет промокодов."
            btns = [[(f"⚙️ {r['code']}", f"adm:pr:{r['code']}")] for r in rows_]
            btns.append([("➕ Создать", "adm:prn")])
            btns.append([("⬅️ Админ-меню", "adm:home")])
            await out(m, True, f"🎟 <b>ПРОМОКОДЫ</b>\n{SEP}\n{body}", kb(btns))

        elif a == "prn":
            await state.set_state(Adm.promo_new)
            await out(m, True, "➕ Отправьте: <code>КОД СУММА ЛИМИТ_АКТИВАЦИЙ</code>\nНапример: <code>WELCOME 2 100</code>",
                      back_kb("adm:promos", "⬅️ Отмена"))

        elif a == "pr":
            t, k_ = promo_card(p[2])
            if not t:
                return await c.answer("Не найден", show_alert=True)
            await out(m, True, t, k_)

        elif a == "prt":
            ex("UPDATE promos SET active=1-active WHERE code=?", (p[2],))
            audit(admin_id, "promo_toggle", p[2])
            t, k_ = promo_card(p[2])
            if not t:
                return await c.answer("Не найден", show_alert=True)
            await out(m, True, t, k_)

        elif a == "prd":
            tok = pend(admin_id, "promo_del", code=p[2])
            await out(m, True, f"🗑 Удалить промокод <b>{esc(p[2])}</b>?", confirm_kb(tok))

        # ---------- broadcast ----------
        elif a == "bc":
            n = q1("SELECT COUNT(*) n FROM users WHERE status!='BANNED'")["n"]
            await state.set_state(Adm.bcast_text)
            await out(m, True, f"📣 <b>РАССЫЛКА</b>\n{SEP}\nПолучателей: <b>{n}</b>\nОтправьте текст сообщения (HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, ссылки).",
                      back_kb("adm:home", "⬅️ Отмена"))

        # ---------- log ----------
        elif a == "log":
            page = max(0, int(p[2]))
            rows = qa("SELECT * FROM audit ORDER BY id DESC LIMIT 11 OFFSET ?", (page * 10,))
            body = "\n".join(f"<code>{r['created_at'][5:16].replace('T', ' ')}</code> {r['actor_id']} · <b>{esc(r['action'])}</b> · {esc(r['details'])[:70]}" for r in rows[:10]) or "Пусто."
            nav = []
            if page > 0:
                nav.append(("⬅️", f"adm:log:{page - 1}"))
            if len(rows) > 10:
                nav.append(("➡️", f"adm:log:{page + 1}"))
            btns = [nav] if nav else []
            btns.append([("⬅️ Админ-меню", "adm:home")])
            await out(m, True, f"📜 <b>ЖУРНАЛ ДЕЙСТВИЙ</b> (стр. {page + 1})\n{SEP}\n{body}", kb(btns))

        # ---------- export ----------
        elif a == "exp":
            await out(m, True, f"📤 <b>ЭКСПОРТ</b>\n{SEP}\nВыберите, что выгрузить:",
                      kb([[("👥 Пользователи CSV", "adm:ex:users"), ("📈 Статистика CSV", "adm:ex:stats")],
                          [("🧾 Леджер CSV", "adm:ex:ledger"), ("🗄 Бэкап БД", "adm:ex:db")],
                          [("⬅️ Админ-меню", "adm:home")]]))

        elif a == "ex":
            what = p[2]
            await c.answer("Готовлю файл…")
            if what == "users":
                rows = qa("""SELECT u.telegram_id, u.username, u.name, u.status, u.vip_level, b.balance, b.total_wagered,
                             COALESCE((SELECT SUM(amount_cents) FROM invoices i WHERE i.user_id=u.telegram_id AND i.status='PAID'),0) dep,
                             COALESCE((SELECT SUM(amount_cents) FROM withdrawals w WHERE w.user_id=u.telegram_id AND w.status='DONE'),0) wd,
                             u.referrer_id, u.created_at, u.last_seen
                             FROM users u JOIN balances b USING(telegram_id)""")
                data = csv_bytes(["id", "username", "name", "status", "vip", "balance_cents", "wagered_cents", "deposits_cents", "withdrawn_cents", "referrer", "created", "last_seen"],
                                 [list(r.values()) for r in rows])
                fn = "users.csv"
            elif what == "stats":
                rows = qa("""SELECT substr(started_at,1,10) d, COUNT(*) rounds, SUM(stake) bets, SUM(payout) payouts, SUM(stake)-SUM(payout) ggr
                             FROM rounds WHERE state!='ACTIVE' GROUP BY d ORDER BY d DESC LIMIT 90""")
                data = csv_bytes(["date", "rounds", "bets_cents", "payouts_cents", "ggr_cents"], [list(r.values()) for r in rows])
                fn = "stats.csv"
            elif what == "ledger":
                rows = qa("SELECT id, telegram_id, delta, balance_before, balance_after, reason, actor_id, round_id, created_at FROM ledger ORDER BY id DESC LIMIT 50000")
                data = csv_bytes(["id", "user", "delta", "before", "after", "reason", "actor", "round", "created"], [list(r.values()) for r in rows])
                fn = "ledger.csv"
            elif what == "db":
                data = db_backup_bytes()
                fn = f"napoleon_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.sqlite3"
            else:
                return
            audit(admin_id, "export", what)
            await m.answer_document(BufferedInputFile(data, filename=fn), caption=f"📤 {fn}")
            return

        # ---------- admins ----------
        elif a == "admins":
            if not is_owner(admin_id):
                return await c.answer("Только для владельцев", show_alert=True)
            rows_ = qa("SELECT telegram_id FROM admins")
            btns = [[(f"🗑 {r['telegram_id']}", f"adm:ard:{r['telegram_id']}")] for r in rows_]
            btns.append([("➕ Добавить админа", "adm:ara")])
            btns.append([("⬅️ Админ-меню", "adm:home")])
            await out(m, True, f"🛡 <b>АДМИНИСТРАТОРЫ</b>\n{SEP}\nВладельцы (из кода): {', '.join(map(str, sorted(OWNER_IDS)))}\n"
                      f"Админы: {', '.join(str(r['telegram_id']) for r in rows_) or '—'}", kb(btns))

        elif a == "ara":
            if not is_owner(admin_id):
                return await c.answer("Только для владельцев", show_alert=True)
            await state.set_state(Adm.admin_add)
            await out(m, True, "➕ Отправьте Telegram ID нового админа:", back_kb("adm:admins", "⬅️ Отмена"))

        elif a == "ard":
            if not is_owner(admin_id):
                return await c.answer("Только для владельцев", show_alert=True)
            tok = pend(admin_id, "admin_rm", uid=int(p[2]))
            await out(m, True, f"🗑 Удалить админа <code>{p[2]}</code>?", confirm_kb(tok))

        # ---------- confirmations ----------
        elif a in ("cf", "cx"):
            tok = p[2]
            pd = PENDING.pop(tok, None)
            if not pd or pd["admin"] != admin_id or pd["exp"] < time.time():
                await out(m, True, "⌛ Действие устарело.", back_kb())
            elif a == "cx":
                await out(m, True, "❌ Отменено.", back_kb())
            else:
                res = await run_pending(pd, admin_id)
                await out(m, True, res, back_kb())

        await c.answer()

    except TelegramBadRequest:
        await c.answer()
    except GameError as e:
        await c.answer(str(e), show_alert=True)
    except (ValueError, IndexError):
        await c.answer("Некорректные данные", show_alert=True)


def csv_bytes(headers, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def db_backup_bytes():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(path)
        src.backup(dst)
        dst.close()
        src.close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ===================== ADMIN: FSM INPUT =====================

@adm.message(Adm.setting_value, F.text)
async def adm_fsm_setting(m: Message, state: FSMContext):
    d = await state.get_data()
    key = d.get("key")
    if key not in SETTINGS:
        await state.clear()
        return
    grp, label, kind, _, _ = SETTINGS[key]
    try:
        val = validate_setting(kind, m.text)
    except (ValueError, InvalidOperation) as e:
        return await m.answer(f"⚠️ {esc(e) if str(e) else 'Неверный формат'}")
    old = S(key)
    set_setting(key, val)
    audit(m.from_user.id, "edit_setting", f"{key}: {'***' if kind == 'secret' else old} -> {'***' if kind == 'secret' else val}")
    if kind == "secret":
        try:
            await m.delete()
        except Exception:
            pass
    await state.clear()
    t, k_ = settings_group_view(grp)
    await m.answer(f"✅ <b>{label}</b> обновлено.\n\n{t}", reply_markup=k_)


@adm.message(Adm.game_value, F.text)
async def adm_fsm_game(m: Message, state: FSMContext):
    d = await state.get_data()
    g, field = d.get("game"), d.get("field")
    if g != "*" and g not in GAMES:
        await state.clear()
        return
    raw = m.text.strip()
    try:
        if field == "title":
            if not raw or len(raw) > 30:
                raise ValueError("1–30 символов")
            val = raw
        elif field == "rtp":
            v = Decimal(raw.replace(",", ".").replace("%", ""))
            if not v.is_finite() or not (1 <= v <= 100):
                raise ValueError("RTP должен быть от 1 до 100")
            val = dstr(v)
        elif field in ("min_stake", "max_stake"):
            cents = parse_money(raw, allow_zero=True)
            if cents is None:
                raise ValueError("Нужно число ≥ 0")
            val = cents
        elif field == "oc":
            v = Decimal(raw.replace(",", "."))
            if not v.is_finite() or v < 0 or v > 1000 or (0 < v < Decimal("1.01")):
                raise ValueError("Коэффициент от 1.01 до 1000, либо 0 — авто")
            val = dstr(v)
        else:
            await state.clear()
            return
    except (ValueError, InvalidOperation) as e:
        return await m.answer(f"⚠️ {esc(e) if str(e) else 'Неверный формат'}")

    await state.clear()
    if field == "oc":
        k = d.get("key")
        if k not in outcome_keys(g):
            return await m.answer("⚠️ Исход не найден")
        set_outcome(g, k, coeff=val)
        audit(m.from_user.id, "edit_outcome", f"{g}.{k} coeff={val}")
        t, k_ = outcome_card(g, k)
        return await m.answer(f"✅ Сохранено.\n\n{t}", reply_markup=k_)
    if g == "*":
        ex("UPDATE game_settings SET rtp=?", (val,))
        audit(m.from_user.id, "edit_game_all", f"rtp={val}")
        return await m.answer(f"✅ RTP всех игр: <b>{val}%</b>\n\n🎮 <b>УПРАВЛЕНИЕ ИГРАМИ</b>\n{SEP}",
                              reply_markup=games_menu_kb())
    ex(f"UPDATE game_settings SET {field}=? WHERE game=?", (val, g))
    audit(m.from_user.id, "edit_game", f"{g}.{field}={val}")
    t, k_ = game_card(g)
    await m.answer(f"✅ Сохранено.\n\n{t}", reply_markup=k_)


@adm.message(Adm.user_search, F.text)
async def adm_fsm_search(m: Message, state: FSMContext):
    q = m.text.strip()
    uid = None
    if q.lstrip("-").isdigit():
        uid = int(q)
    else:
        r = q1("SELECT telegram_id FROM users WHERE username=? COLLATE NOCASE OR name LIKE ? ORDER BY last_seen DESC LIMIT 1",
               (q.lstrip("@"), f"%{q}%"))
        uid = r["telegram_id"] if r else None
    t, k_ = user_card(uid) if uid else (None, None)
    if not t:
        return await m.answer("⚠️ Игрок не найден. Попробуйте ещё раз.")
    await state.clear()
    await m.answer(t, reply_markup=k_)


@adm.message(Adm.user_amount, F.text)
async def adm_fsm_amount(m: Message, state: FSMContext):
    d = await state.get_data()
    uid, sign = d["uid"], d["sign"]
    parts = m.text.strip().split(maxsplit=1)
    cents = parse_money(parts[0].lstrip("+-"), allow_zero=(sign == "="))
    if cents is None:
        return await m.answer("⚠️ Неверная сумма. Формат: <code>5 причина</code>")
    reason = parts[1] if len(parts) > 1 else "корректировка"
    u = get_user(uid)
    if not u:
        await state.clear()
        return await m.answer("⚠️ Игрок не найден")
    if sign == "=":
        kw = {"set_to": cents, "delta": cents - u["balance"]}
        desc = f"установить баланс <b>{money(cents)}</b>"
    else:
        delta = cents if sign == "+" else -cents
        kw = {"delta": delta}
        desc = f"изменить баланс на <b>{delta / 100:+.2f} USDT</b>"
    tok = pend(m.from_user.id, "bal", uid=uid, reason=reason[:200], **kw)
    await state.clear()
    await m.answer(f"⚠️ <b>Подтвердите</b>\nИгрок <code>{uid}</code> (сейчас {money(u['balance'])}): {desc}\nПричина: {esc(reason)}",
                   reply_markup=confirm_kb(tok))


@adm.message(Adm.user_field, F.text)
async def adm_fsm_userfield(m: Message, state: FSMContext):
    d = await state.get_data()
    uid, field = d["uid"], d["field"]
    raw = m.text.strip()
    if field == "max_stake":
        cents = parse_money(raw, allow_zero=True)
        if cents is None:
            return await m.answer("⚠️ Нужно число ≥ 0")
        ex("UPDATE users SET max_stake=? WHERE telegram_id=?", (cents, uid))
    else:
        ex("UPDATE users SET note=? WHERE telegram_id=?", ("" if raw == "-" else raw[:500], uid))
    audit(m.from_user.id, f"user_{field}", f"{uid}: {raw[:100]}")
    await state.clear()
    t, k_ = user_card(uid)
    await m.answer(t, reply_markup=k_)


@adm.message(Adm.user_msg, F.text)
async def adm_fsm_usermsg(m: Message, state: FSMContext):
    d = await state.get_data()
    uid = d["uid"]
    await state.clear()
    try:
        await bot.send_message(uid, f"📩 <b>Сообщение от администрации</b>\n{SEP}\n{m.html_text}")
        audit(m.from_user.id, "user_msg", uid)
        await m.answer("✅ Отправлено.", reply_markup=back_kb(f"adm:u:{uid}", "⬅️ Карточка"))
    except Exception as e:
        await m.answer(f"⚠️ Не удалось отправить: {esc(e)}")


@adm.message(Adm.bcast_text, F.text)
async def adm_fsm_bcast(m: Message, state: FSMContext):
    text = m.html_text
    n = q1("SELECT COUNT(*) n FROM users WHERE status!='BANNED'")["n"]
    tok = pend(m.from_user.id, "bcast", text=text)
    await state.clear()
    await m.answer(f"📣 <b>Предпросмотр</b> (получателей: {n})\n{SEP}\n{text}", reply_markup=confirm_kb(tok))


@adm.message(Adm.promo_new, F.text)
async def adm_fsm_promo(m: Message, state: FSMContext):
    parts = m.text.split()
    if len(parts) != 3 or not re.fullmatch(r"[A-Za-z0-9_-]{3,24}", parts[0]):
        return await m.answer("⚠️ Формат: <code>КОД СУММА ЛИМИТ</code> (код 3–24 символа)")
    cents = parse_money(parts[1])
    try:
        lim = int(parts[2])
    except ValueError:
        lim = 0
    if cents is None or lim < 1:
        return await m.answer("⚠️ Сумма > 0 и лимит ≥ 1")
    code = parts[0].upper()
    try:
        ex("INSERT INTO promos(code, amount, max_uses, created_at) VALUES(?,?,?,?)", (code, cents, lim, now()))
    except sqlite3.IntegrityError:
        return await m.answer("⚠️ Такой код уже есть")
    audit(m.from_user.id, "promo_new", f"{code} {cents} x{lim}")
    await state.clear()
    await m.answer(f"✅ Промокод <code>{code}</code>: {money(cents)} × {lim}", reply_markup=back_kb("adm:promos", "⬅️ Промокоды"))


@adm.message(Adm.admin_add, F.text)
async def adm_fsm_admin(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        return
    if not m.text.strip().isdigit():
        return await m.answer("⚠️ Нужен числовой ID")
    uid = int(m.text.strip())
    ex("INSERT OR IGNORE INTO admins(telegram_id, added_by, created_at) VALUES(?,?,?)", (uid, m.from_user.id, now()))
    load_admins()
    audit(m.from_user.id, "admin_add", uid)
    await state.clear()
    await m.answer(f"✅ Админ {uid} добавлен.", reply_markup=back_kb("adm:admins", "⬅️ Админы"))


# ===================== ADMIN: COMMANDS =====================

@adm.message(Command("setbalance"))
async def cmd_setbalance(m: Message):
    parts = (m.text or "").split(maxsplit=3)
    if len(parts) != 4 or not parts[1].isdigit():
        return await m.answer("Формат: <code>/setbalance ID +10 причина</code> или <code>-2.5</code>")
    sign = -1 if parts[2].startswith("-") else 1
    cents = parse_money(parts[2].lstrip("+-"))
    if cents is None:
        return await m.answer("⚠️ Неверная сумма.")
    try:
        b, a = change_balance(int(parts[1]), sign * cents, parts[3], m.from_user.id)
        await m.answer(f"✅ Было: {money(b)} → Стало: {money(a)}")
        await notify_user(int(parts[1]), f"🎩 Администратор изменил ваш баланс на <b>{sign * cents / 100:+.2f} USDT</b>.\nПричина: {esc(parts[3])}")
    except GameError as e:
        await m.answer(f"⚠️ {e}")


@adm.message(Command("set"))
async def cmd_set(m: Message):
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 3 or parts[1] not in SETTINGS:
        return await m.answer("Формат: <code>/set ключ значение</code>. Всё лучше менять через /admin → Настройки.")
    try:
        val = validate_setting(SETTINGS[parts[1]][2], parts[2])
    except (ValueError, InvalidOperation) as e:
        return await m.answer(f"⚠️ {esc(e)}")
    old = S(parts[1])
    set_setting(parts[1], val)
    audit(m.from_user.id, "edit_setting", f"{parts[1]}: {old} -> {val}")
    await m.answer(f"✅ {parts[1]} = <code>{esc(val)}</code>")


# ===================== BROADCAST =====================

BG_TASKS: set = set()


async def do_broadcast(admin_id, text):
    ids = [r["telegram_id"] for r in qa("SELECT telegram_id FROM users WHERE status!='BANNED'")]
    ok = fail = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(uid, text)
                ok += 1
            except Exception:
                fail += 1
        except TelegramForbiddenError:
            fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    audit(admin_id, "broadcast", f"ok={ok} fail={fail}")
    await notify_user(admin_id, f"📣 <b>Рассылка завершена</b>\n✅ Доставлено: {ok}\n❌ Не доставлено: {fail}")


# ===================== ERROR HANDLER =====================

@dp.errors()
async def on_error(event: ErrorEvent):
    log.error("Update error: %s", event.exception, exc_info=event.exception)
    try:
        cq = event.update.callback_query
        if cq:
            await cq.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
        elif event.update.message:
            await event.update.message.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.")
    except Exception:
        pass
    return True


# ===================== BACKGROUND WORKERS =====================

async def run_loop(name, interval, fn):
    while True:
        HB[name] = time.time()
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("loop %s", name)
        await asyncio.sleep(interval)


async def invoices_tick():
    rows = qa("SELECT invoice_id FROM invoices WHERE status='PENDING' AND created_at <= ? ORDER BY created_at LIMIT 40",
              (since(seconds=5),))
    for r in rows:
        await process_invoice(r["invoice_id"], notify=True)


async def rounds_tick():
    timeout = max(1, Sn("round_timeout"))
    for r in qa("SELECT * FROM rounds WHERE state='ACTIVE' AND started_at <= ?", (since(minutes=timeout),)):
        if r["user_id"] in busy_users:
            continue
        async with round_locks[r["round_id"]]:
            cur = get_round(r["round_id"])
            if not cur or cur["state"] != "ACTIVE":
                continue
            if cur["game"] in ("mines", "tower"):
                step, mult = round_progress(cur)
                if step >= 1:
                    net = finish_round(cur["round_id"], "CASHED_OUT", int(Decimal(cur["stake"]) * mult), "timeout_cashout")
                    if net is not None:
                        await notify_user(cur["user_id"], f"⏱ Раунд закрыт по таймауту — выигрыш автоматически забран: <b>{money(net)}</b>.")
                    continue
            net = refund_round(cur["round_id"], "timeout_refund")
            if net is not None:
                await notify_user(cur["user_id"], "⏱ Раунд закрыт по таймауту. Ставка возвращена.")


async def payout_tick():
    rows = qa("SELECT * FROM withdrawals WHERE status='PENDING' AND provider='cryptobot' ORDER BY id LIMIT 5")
    for w in rows:
        with tx() as c:
            if c.execute("UPDATE withdrawals SET status='PROCESSING' WHERE id=? AND status='PENDING'", (w["id"],)).rowcount == 0:
                continue
        wid = w["id"]
        try:
            check_url = await cryptobot_create_check(w["user_id"], w["amount_cents"])
        except GameError as e:
            msg = str(e)
            kind = cb_error_kind(msg)
            if kind == "user":
                finalize_withdrawal(wid, False)
                await notify_user(
                    w["user_id"],
                    f"❌ Заявка №{wid} не выполнена: CryptoBot не смог создать чек ({esc(msg)}). "
                    f"Средства возвращены на баланс.",
                )
                await notify_admins(f"❌ Создание чека для вывода #{wid} отклонено: {esc(msg)}")
            else:
                note = ("Нехватка средств у провайдера: " if kind == "funds" else "Ошибка настройки CryptoBot: ") + msg
                ex("UPDATE withdrawals SET status='REVIEW', admin_note=? WHERE id=? AND status='PROCESSING'", (note[:300], wid))
                await notify_admins(
                    f"⚠️ Автовывод-чек #{wid} ({money(w['amount_cents'])}) остановлен: {esc(note)}\n"
                    f"Ответ CryptoBot: {esc(msg)}. Проверьте баланс приложения, токен, разрешения и лимиты.",
                    kb([[("Открыть заявку", f"adm:wd:{wid}")]]),
                )
                await notify_user(w["user_id"], f"⏳ Заявка №{wid} задержана и передана администратору. Средства зарезервированы.")
            continue
        except Exception as e:
            # Не повторяем createCheck при неопределённом результате: иначе можно выдать два чека.
            log.exception("check creation result unknown")
            ex("UPDATE withdrawals SET status='UNKNOWN', admin_note=? WHERE id=? AND status='PROCESSING'",
               (f"Результат createCheck неизвестен; автоматический повтор запрещён: {str(e)[:220]}"[:300], wid))
            await notify_admins(
                f"⚠️ Неизвестный результат создания чека для заявки #{wid}.\n"
                f"Игрок: {w['user_id']}\nСумма: {money(w['amount_cents'])}\n"
                f"Повтор автоматически не выполнялся, чтобы не создать второй чек. Нужна ручная сверка.",
                kb([[("Открыть заявку", f"adm:wd:{wid}")]]),
            )
            await notify_user(w["user_id"], f"⚠️ Заявка №{wid} ожидает проверки результата создания чека. Не создавайте повторную заявку.")
            continue

        try:
            finalize_withdrawal(wid, True, None, check_url)
        except Exception as e:
            log.exception("finalize after check creation failed")
            ex("UPDATE withdrawals SET status='UNKNOWN', tx_id=?, admin_note=? WHERE id=?",
               (check_url, f"Чек создан ({check_url}), но учёт не завершён: {e}"[:300], wid))
            await notify_admins(
                f"🚨 Чек для вывода #{wid} создан, но списание в БД не завершено.\n"
                f"Ссылка: {esc(check_url)}\nТребуется сверить учёт перед любыми действиями."
            )
            continue
        await notify_user(
            w["user_id"],
            f"✅ <b>Чек для вывода №{wid} создан!</b>\n"
            f"Сумма: <b>{money(w['amount_cents'])}</b>\n"
            f"Получить чек: {esc(check_url)}\n\n"
            f"Чек закреплён за вашим Telegram ID и не предназначен для передачи другим пользователям."
        )


async def unknown_tick():
    """Автосверка выплат со статусом UNKNOWN: если перевод найден в CryptoBot — завершаем учёт."""
    rows = qa("SELECT * FROM withdrawals WHERE status='UNKNOWN' AND provider='cryptobot' ORDER BY id LIMIT 10")
    for w in rows:
        wid = w["id"]
        txid = await cryptobot_find_transfer(f"wd{wid}")
        if not txid:
            continue
        done = finalize_withdrawal(wid, True, None, txid, from_states=("UNKNOWN",))
        if done:
            audit(None, "wd_auto_reconcile", f"#{wid} tx {txid}")
            await notify_user(w["user_id"], f"✅ <b>Заявка №{wid} выполнена!</b>\nСумма: <b>{money(w['amount_cents'])}</b>\nTX: <code>{esc(txid)}</code>")
            await notify_admins(f"✅ Выплата #{wid} найдена в CryptoBot (tx {esc(txid)}) и закрыта автоматически.")


async def notify_big_wins_loop():
    while True:
        HB["wins_channel"] = time.time()
        try:
            name, game, net = await asyncio.wait_for(win_queue.get(), timeout=60)
        except asyncio.TimeoutError:
            continue
        try:
            ch = S("wins_channel_id")
            if ch:
                await bot.send_message(
                    ch, f"🏆 <b>{esc(name)}</b> выиграл <b>{money(net)}</b> в {gtitle(game) if game in GAMES else game}!")
        except Exception:
            log.warning("не удалось отправить уведомление в канал", exc_info=True)
        await asyncio.sleep(1)


# ===================== HEALTH SERVER =====================

async def handle_http(reader, writer):
    try:
        await asyncio.wait_for(reader.read(1024), timeout=5)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def start_web_server():
    server = await asyncio.start_server(handle_http, "0.0.0.0", PORT)
    log.info("HTTP health server on port %s", PORT)
    async with server:
        await server.serve_forever()


# ===================== MAIN =====================

async def main():
    init_db()

    guard = Guard()
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(adm)
    dp.include_router(deny)

    log.info("Запуск Napoleon Casino... CryptoBot=%s", bool(cb_token()))

    tasks = [
        start_web_server(),
        run_loop("invoices", 10, invoices_tick),
        run_loop("rounds", 60, rounds_tick),
        run_loop("payouts", 5, payout_tick),
        run_loop("reconcile", 60, unknown_tick),
        notify_big_wins_loop(),
    ]
    bg = [asyncio.create_task(t) for t in tasks]

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in bg:
            t.cancel()
        await asyncio.gather(*bg, return_exceptions=True)
        for t in list(BG_TASKS):
            t.cancel()
        try:
            if _client and not _client.is_closed:
                await _client.aclose()
        except Exception:
            pass
        await bot.session.close()
        log.info("Остановлено корректно.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
