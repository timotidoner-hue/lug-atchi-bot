import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, quote
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("leitner_auto_sub")

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", 8080))
DATA_DIR = (
    os.getenv("DATA_DIR")
    or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    or ("/data" if os.path.isdir("/data") else ".")
)
TZ = ZoneInfo(os.getenv("BOT_TZ", "Asia/Tashkent"))

BASE_PERSONAL_LIMIT = 30
REFERRAL_BONUS_LIMIT = 50
SCORE_CORRECT = 10
SCORE_WRONG = -5
SUB_WATCH_SECONDS = 10
AUTO_HOUR = 16
AUTO_COUNT = 10
DAILY_LIMIT = 50


def parse_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    ids: set[int] = set()
    for part in re.split(r"[,\s;]+", raw.strip()):
        if part.isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS = parse_admin_ids()

LEITNER_INTERVALS = {
    0: 600,
    1: 86400,
    2: 3 * 86400,
    3: 7 * 86400,
    4: 16 * 86400,
    5: 35 * 86400,
}

os.makedirs(DATA_DIR, exist_ok=True)
db = sqlite3.connect(os.path.join(DATA_DIR, "bot.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL;")
db.execute("PRAGMA synchronous=NORMAL;")

db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    full_name TEXT DEFAULT '',
    username TEXT DEFAULT '',
    referred_by INTEGER,
    referral_count INTEGER DEFAULT 0,
    bonus_limit INTEGER DEFAULT 0,
    score INTEGER DEFAULT 0,
    streak INTEGER DEFAULT 0,
    last_active TEXT,
    day TEXT,
    day_count INTEGER DEFAULT 0,
    last_auto TEXT
);
CREATE TABLE IF NOT EXISTS mandatory_channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT DEFAULT '',
    chat_ref TEXT NOT NULL,
    url TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    scope TEXT DEFAULT 'personal',
    h TEXT,
    question TEXT,
    options TEXT,
    correct INTEGER,
    box INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    correct_count INTEGER DEFAULT 0,
    seen INTEGER DEFAULT 0,
    last_ok INTEGER DEFAULT 0,
    due INTEGER DEFAULT 0,
    last_ts INTEGER DEFAULT 0,
    UNIQUE(user_id, scope, h)
);
CREATE TABLE IF NOT EXISTS sessions(
    user_id INTEGER PRIMARY KEY,
    current_q_id INTEGER DEFAULT 0,
    remaining INTEGER,
    total INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 0,
    mode TEXT,
    started INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_drafts(
    draft_id TEXT PRIMARY KEY,
    user_id INTEGER,
    scope TEXT DEFAULT 'personal',
    question TEXT,
    options TEXT,
    created_ts INTEGER
);
"""
)

for _sql in (
    "ALTER TABLE users ADD COLUMN full_name TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN username TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN referred_by INTEGER",
    "ALTER TABLE users ADD COLUMN referral_count INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN bonus_limit INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN score INTEGER DEFAULT 0",
    "ALTER TABLE questions ADD COLUMN scope TEXT DEFAULT 'personal'",
    "ALTER TABLE questions ADD COLUMN box INTEGER DEFAULT 0",
    "ALTER TABLE questions ADD COLUMN correct_count INTEGER DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN current_q_id INTEGER DEFAULT 0",
    "ALTER TABLE pending_drafts ADD COLUMN scope TEXT DEFAULT 'personal'",
):
    try:
        db.execute(_sql)
    except sqlite3.OperationalError:
        pass
db.commit()

watch_tasks: dict[tuple[str, int], asyncio.Task] = {}
webapp_watch_state: dict[int, dict] = {}


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def now_dt():
    return datetime.now(TZ)


def today():
    return now_dt().date().isoformat()


def sync_global_questions_for_user(uid: int):
    globals_rows = db.execute("SELECT * FROM questions WHERE user_id=0 AND scope='global'").fetchall()
    for g in globals_rows:
        try:
            db.execute(
                "INSERT INTO questions(user_id,scope,h,question,options,correct,box,due) VALUES(?,?,?,?,?,?,0,0)",
                (uid, "global", g["h"], g["question"], g["options"], g["correct"]),
            )
        except sqlite3.IntegrityError:
            pass
    db.commit()


def ensure_user(uid: int, full_name: str = "", username: str = "", ref_by: int | None = None):
    row = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        la = today() if now_dt().hour >= AUTO_HOUR else None
        valid_ref = None
        if ref_by and ref_by != uid:
            if db.execute("SELECT user_id FROM users WHERE user_id=?", (ref_by,)).fetchone():
                valid_ref = ref_by
        db.execute(
            "INSERT INTO users(user_id, full_name, username, referred_by, last_auto) VALUES(?,?,?,?,?)",
            (uid, full_name, username, valid_ref, la),
        )
        if valid_ref:
            db.execute(
                "UPDATE users SET referral_count = referral_count + 1, bonus_limit = bonus_limit + ? WHERE user_id=?",
                (REFERRAL_BONUS_LIMIT, valid_ref),
            )
        db.commit()
        sync_global_questions_for_user(uid)
        return True

    updates, params = [], []
    if full_name and row["full_name"] != full_name:
        updates.append("full_name=?")
        params.append(full_name)
    if username and row["username"] != username:
        updates.append("username=?")
        params.append(username)
    if updates:
        params.append(uid)
        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id=?", params)
        db.commit()
    return False


def touch_streak(uid: int):
    u = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not u:
        return
    t = today()
    if u["last_active"] == t:
        return
    y = (now_dt().date() - timedelta(days=1)).isoformat()
    streak = u["streak"] + 1 if u["last_active"] == y else 1
    db.execute("UPDATE users SET streak=?, last_active=? WHERE user_id=?", (streak, t, uid))
    db.commit()


def streak_now(uid: int) -> int:
    u = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not u or not u["last_active"]:
        return 0
    y = (now_dt().date() - timedelta(days=1)).isoformat()
    return u["streak"] if u["last_active"] in (today(), y) else 0


def day_count(uid: int) -> int:
    u = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    return u["day_count"] if u and u["day"] == today() else 0


def bump_day(uid: int):
    c = day_count(uid) + 1
    db.execute("UPDATE users SET day=?, day_count=? WHERE user_id=?", (today(), c, uid))
    db.commit()


def personal_count(uid: int) -> int:
    return db.execute(
        "SELECT COUNT(*) c FROM questions WHERE user_id=? AND scope='personal'", (uid,)
    ).fetchone()["c"]


def personal_limit(uid: int) -> int:
    if is_admin(uid):
        return 999999
    u = db.execute("SELECT bonus_limit FROM users WHERE user_id=?", (uid,)).fetchone()
    bonus = u["bonus_limit"] if u and u["bonus_limit"] else 0
    return BASE_PERSONAL_LIMIT + bonus


def personal_remaining(uid: int) -> int:
    if is_admin(uid):
        return 999999
    return max(0, personal_limit(uid) - personal_count(uid))


def get_user_score(uid: int) -> int:
    u = db.execute("SELECT score FROM users WHERE user_id=?", (uid,)).fetchone()
    return int(u["score"]) if u and u["score"] is not None else 0


def apply_score_delta(uid: int, delta: int) -> int:
    new_score = max(0, get_user_score(uid) + delta)
    db.execute("UPDATE users SET score=? WHERE user_id=?", (new_score, uid))
    db.commit()
    return new_score


def normalize_channel_input(raw_chat: str, raw_url: str = "", raw_title: str = "") -> tuple[str, str, str]:
    c = (raw_chat or "").strip()
    u = (raw_url or "").strip()
    t = (raw_title or "").strip()
    if not c:
        raise ValueError("Укажите канал (@username, ссылку или ID канала)")

    m = re.match(r"^https?://(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)/?$", c)
    if m:
        uname = m.group(1)
        c = f"@{uname}"
        if not u:
            u = f"https://t.me/{uname}"

    if re.match(r"^@[A-Za-z0-9_]{3,}$", c):
        if not u:
            u = f"https://t.me/{c[1:]}"
    elif re.match(r"^-100\d+$", c):
        if not u:
            raise ValueError("Для числового ID канала укажите ссылку https://t.me/...")
    else:
        raise ValueError("Некорректный формат канала. Используйте @username, ссылку или -100...")

    if not re.match(r"^https?://", u):
        raise ValueError("Ссылка должна начинаться с https://")
    return (t or c), c, u


def list_mandatory_channels() -> list[dict]:
    rows = db.execute("SELECT id, title, chat_ref, url FROM mandatory_channels ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


async def check_user_subscriptions(bot: Bot, uid: int) -> tuple[bool, list[dict]]:
    if is_admin(uid):
        return True, []
    channels = list_mandatory_channels()
    if not channels:
        return True, []

    missing = []
    allowed = {"member", "administrator", "creator"}
    for ch in channels:
        target = int(ch["chat_ref"]) if re.match(r"^-100\d+$", ch["chat_ref"]) else ch["chat_ref"]
        try:
            member = await bot.get_chat_member(chat_id=target, user_id=uid)
            if getattr(member, "status", "") not in allowed:
                missing.append(ch)
        except Exception as e:
            log.warning("Ошибка проверки подписки на %s для %s: %s", ch["chat_ref"], uid, e)
            missing.append(ch)
    return (len(missing) == 0), missing


def build_subscribe_keyboard(missing_channels: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for ch in missing_channels:
        rows.append([
            InlineKeyboardButton(
                text=f"📢 Подписаться: {ch['title']}",
                url=ch["url"],
                callback_data=f"sublink:{ch['id']}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def run_chat_subscription_watch(bot: Bot, chat_id: int, uid: int):
    key = ("chat", uid)
    try:
        for _ in range(SUB_WATCH_SECONDS):
            await asyncio.sleep(1)
            ok_sub, _ = await check_user_subscriptions(bot, uid)
            if ok_sub:
                await bot.send_message(
                    chat_id,
                    "✅ <b>Вы подписались! Доступ к боту открыт.</b>",
                    reply_markup=get_menu(),
                    parse_mode="HTML",
                )
                return
        await bot.send_message(
            chat_id,
            f"⏱ Вы не подписались за {SUB_WATCH_SECONDS} секунд. Перейдите по ссылке канала и подпишитесь, чтобы продолжить.",
        )
    except asyncio.CancelledError:
        pass
    finally:
        watch_tasks.pop(key, None)


def start_chat_watch_if_needed(bot: Bot, chat_id: int, uid: int):
    key = ("chat", uid)
    old = watch_tasks.get(key)
    if old and not old.done():
        return
    watch_tasks[key] = asyncio.create_task(run_chat_subscription_watch(bot, chat_id, uid))
JSON_FORMAT_EXAMPLE = """[
  {
    "question": "Столица России?",
    "options": ["Москва", "Санкт-Петербург", "Казань", "Новосибирск"],
    "answer": "Москва"
  }
]"""

LIST_KEYS = ("questions", "tests", "data", "items", "вопросы", "тесты", "карточки")
Q_KEYS = ("question", "q", "text", "вопрос", "текст", "задание")
O_KEYS = ("options", "variants", "choices", "answers", "варианты", "ответы", "выбор")
A_KEYS = ("answer", "correct", "correct_answer", "ответ", "верный", "правильный")
CYR_LETTERS = "АБВГДЕ"


def norm(s):
    s = unicodedata.normalize("NFC", str(s)).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


def decode_bytes(raw):
    for enc in ("utf-8-sig", "utf-8", "cp1251", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError("Неизвестная кодировка файла (используйте UTF-8)")


def parse_plain_lines(raw_text: str):
    lines = [norm(line) for line in raw_text.replace("\r\n", "\n").split("\n") if norm(line)]
    if len(lines) < 3:
        raise ValueError("Введите вопрос и минимум 2 варианта ответа (каждый с новой строки)")
    if len(lines) > 7:
        raise ValueError("Максимум 1 вопрос и 6 вариантов ответа")
    question = lines[0]
    options = lines[1:]
    if len({o.lower() for o in options}) < len(options):
        raise ValueError("Варианты ответа не должны повторяться")
    return question, options


def pick(d, keys):
    low = {norm(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in low:
            return low[k.lower()]
    return None


def find_correct(opts, letters, ans):
    n = len(opts)
    if ans is None or isinstance(ans, bool):
        raise ValueError("не указан правильный ответ")
    if isinstance(ans, int):
        if 1 <= ans <= n:
            return ans - 1
        raise ValueError("неверный номер правильного ответа")
    s = norm(ans)
    for i, o in enumerate(opts):
        if o.lower() == s.lower():
            return i
    s = s.rstrip(").:").strip()
    if letters and s.upper() in letters:
        return letters.index(s.upper())
    if len(s) == 1 and s.upper() in CYR_LETTERS[:n]:
        return CYR_LETTERS.index(s.upper())
    if len(s) == 1 and s.upper() in "ABCDEF"[:n]:
        return "ABCDEF".index(s.upper())
    if s.isdigit() and 1 <= int(s) <= n:
        return int(s) - 1
    raise ValueError("не удалось определить правильный ответ")


def parse_one(it):
    if not isinstance(it, dict):
        raise ValueError("элемент не является объектом")
    q = pick(it, Q_KEYS)
    raw = pick(it, O_KEYS)
    ans = pick(it, A_KEYS)
    if not isinstance(q, str) or not q.strip():
        raise ValueError("не найден текст вопроса")
    letters = None
    if isinstance(raw, dict):
        letters = [norm(k).upper() for k in raw]
        opts = [norm(v) for v in raw.values()]
    elif isinstance(raw, list):
        opts = [norm(v) for v in raw]
    else:
        raise ValueError("не найдены варианты ответа")
    if not 2 <= len(opts) <= 6 or any(not o for o in opts):
        raise ValueError("неверное количество или текст вариантов")
    if len({o.lower() for o in opts}) < len(opts):
        raise ValueError("варианты ответа повторяются")
    return norm(q), opts, find_correct(opts, letters, ans)


def parse_items(data):
    if isinstance(data, dict):
        lst = pick(data, LIST_KEYS)
        data = lst if isinstance(lst, list) else [data]
    if not isinstance(data, list):
        raise ValueError("JSON должен содержать список вопросов")
    items, errors = [], []
    for n, it in enumerate(data, 1):
        try:
            items.append(parse_one(it))
        except Exception as e:
            errors.append(f"#{n}: {e}")
    return items, errors


def qhash(q):
    t = "".join(ch for ch in norm(q).lower() if ch.isalnum())
    return hashlib.sha1(t.encode()).hexdigest()


def add_questions_with_scope(uid: int, items: list, scope: str = "personal"):
    target_scope = "global" if (scope == "global" and is_admin(uid)) else "personal"
    added = dup = skipped_limit = 0
    allowed = personal_remaining(uid) if target_scope == "personal" else len(items)

    for q, opts, correct in items:
        if target_scope == "personal" and added >= allowed:
            skipped_limit += 1
            continue
        h = qhash(q)
        if target_scope == "global":
            try:
                db.execute(
                    "INSERT INTO questions(user_id,scope,h,question,options,correct,box,due) VALUES(0,'global',?,?,?,?,0,0)",
                    (h, q, json.dumps(opts, ensure_ascii=False), correct),
                )
            except sqlite3.IntegrityError:
                pass
            all_users = db.execute("SELECT user_id FROM users").fetchall()
            user_added = False
            for u in all_users:
                try:
                    db.execute(
                        "INSERT INTO questions(user_id,scope,h,question,options,correct,box,due) VALUES(?,'global',?,?,?,?,0,0)",
                        (u["user_id"], h, q, json.dumps(opts, ensure_ascii=False), correct),
                    )
                    user_added = True
                except sqlite3.IntegrityError:
                    pass
            if user_added:
                added += 1
            else:
                dup += 1
        else:
            try:
                db.execute(
                    "INSERT INTO questions(user_id,scope,h,question,options,correct,box,due) VALUES(?,'personal',?,?,?,?,0,0)",
                    (uid, h, q, json.dumps(opts, ensure_ascii=False), correct),
                )
                added += 1
            except sqlite3.IntegrityError:
                dup += 1

    db.commit()
    return {"added": added, "dup": dup, "skipped_limit": skipped_limit, "scope": target_scope}


def create_draft(uid: int, question: str, options: list[str], scope: str = "personal") -> str:
    draft_id = hashlib.sha1(f"{uid}:{time.time_ns()}:{random.random()}".encode()).hexdigest()[:12]
    db.execute(
        "INSERT INTO pending_drafts(draft_id,user_id,scope,question,options,created_ts) VALUES(?,?,?,?,?,?)",
        (draft_id, uid, scope, question, json.dumps(options, ensure_ascii=False), int(time.time())),
    )
    db.commit()
    return draft_id


def apply_leitner_answer(uid: int, qid: int, is_ok: bool):
    q = db.execute("SELECT * FROM questions WHERE id=? AND user_id=?", (qid, uid)).fetchone()
    if not q:
        return None
    now_ts = int(time.time())
    old_box = q["box"] or 0
    new_box = min(old_box + 1, 5) if is_ok else 0
    next_due = now_ts + LEITNER_INTERVALS[new_box]
    delta = SCORE_CORRECT if is_ok else SCORE_WRONG
    new_score = apply_score_delta(uid, delta)

    db.execute(
        "UPDATE questions SET box=?, attempts=attempts+1, correct_count=correct_count+?, "
        "seen=1, last_ok=?, due=?, last_ts=? WHERE id=?",
        (new_box, 1 if is_ok else 0, 1 if is_ok else 0, next_due, now_ts, qid),
    )
    db.commit()
    touch_streak(uid)
    return {"old_box": old_box, "new_box": new_box, "next_due": next_due, "delta": delta, "score": new_score}


def pick_question(uid: int, mode: str = "due", exclude_ids=None):
    exclude_ids = exclude_ids or []
    placeholders = ",".join("?" * len(exclude_ids)) if exclude_ids else "0"
    now_ts = int(time.time())
    if mode == "wrong":
        query = (
            f"SELECT * FROM questions WHERE user_id=? AND seen=1 AND last_ok=0 "
            f"AND id NOT IN ({placeholders}) ORDER BY last_ts ASC, RANDOM() LIMIT 1"
        )
        return db.execute(query, (uid, *exclude_ids)).fetchone()
    query = (
        f"SELECT * FROM questions WHERE user_id=? AND due<=? "
        f"AND id NOT IN ({placeholders}) ORDER BY box ASC, seen ASC, RANDOM() LIMIT 1"
    )
    return db.execute(query, (uid, now_ts, *exclude_ids)).fetchone()


def build_leaderboard(limit: int = 20) -> list[dict]:
    users = db.execute("SELECT user_id, full_name, username, score FROM users").fetchall()
    board = []
    for u in users:
        uid = u["user_id"]
        agg = db.execute(
            "SELECT COALESCE(SUM(attempts),0) att, COALESCE(SUM(correct_count),0) cor "
            "FROM questions WHERE user_id=?",
            (uid,),
        ).fetchone()
        acc = round((agg["cor"] / agg["att"]) * 100) if agg["att"] > 0 else 0
        board.append({
            "user_id": uid,
            "name": u["full_name"] or f"Пользователь {uid}",
            "username": u["username"] or "",
            "score": int(u["score"] or 0),
            "correct": int(agg["cor"]),
            "accuracy": acc,
            "streak": streak_now(uid),
        })
    board.sort(key=lambda x: (-x["score"], -x["correct"], x["user_id"]))
    for idx, item in enumerate(board[:limit], 1):
        item["rank"] = idx
    return board[:limit]


def build_admin_users_list() -> list[dict]:
    users = db.execute("SELECT * FROM users ORDER BY user_id DESC").fetchall()
    result = []
    for u in users:
        uid = u["user_id"]
        total_q = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=?", (uid,)).fetchone()["c"]
        own_q = db.execute(
            "SELECT COUNT(*) c FROM questions WHERE user_id=? AND scope='personal'", (uid,)
        ).fetchone()["c"]
        agg = db.execute(
            "SELECT COALESCE(SUM(attempts),0) att, COALESCE(SUM(correct_count),0) cor "
            "FROM questions WHERE user_id=?",
            (uid,),
        ).fetchone()
        boxes = {str(i): 0 for i in range(6)}
        for r in db.execute("SELECT box, COUNT(*) c FROM questions WHERE user_id=? GROUP BY box", (uid,)).fetchall():
            boxes[str(r["box"])] = r["c"]
        acc = round((agg["cor"] / agg["att"]) * 100) if agg["att"] > 0 else 0
        result.append({
            "user_id": uid,
            "name": u["full_name"] or f"Пользователь {uid}",
            "username": u["username"] or "",
            "streak": streak_now(uid),
            "accuracy": acc,
            "score": int(u["score"] or 0),
            "total_questions": total_q,
            "own_questions": own_q,
            "personal_limit": personal_limit(uid),
            "referral_count": u["referral_count"] or 0,
            "boxes": boxes,
        })
    result.sort(key=lambda x: (-x["score"], -x["total_questions"]))
    return result


def get_referral_link(bot_username: str | None, uid: int) -> str:
    uname = (bot_username or os.getenv("BOT_USERNAME", "your_bot")).lstrip("@")
    return f"https://t.me/{uname}?start=ref_{uid}"


def get_share_url(bot_username: str | None, uid: int) -> str:
    link = get_referral_link(bot_username, uid)
    text = f"Присоединяйся к тренажёру по методу Лейтнера и Эббингауза! +50 тестов по ссылке: {link}"
    return f"https://t.me/share/url?url={quote(link)}&text={quote(text)}"


BTN_WORK = "📝 Пройти тест"
BTN_WRONG = "❌ Работа над ошибками"
BTN_ADD = "➕ Добавить тест"
BTN_STATS = "📊 Статистика"


def get_menu():
    rows = [
        [KeyboardButton(text=BTN_WORK), KeyboardButton(text=BTN_WRONG)],
        [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_STATS)],
    ]
    if WEBAPP_URL:
        rows.insert(0, [KeyboardButton(text="🚀 Открыть Mini App", web_app=WebAppInfo(url=WEBAPP_URL))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


router = Router()


async def prompt_subscribe_chat(bot: Bot, chat_id: int, uid: int, missing: list[dict]):
    await bot.send_message(
        chat_id,
        "🔒 <b>Для доступа подпишитесь на обязательный канал.</b>\n"
        f"Нажмите на ссылку ниже — бот начнёт проверять подписку каждую секунду (в течение {SUB_WATCH_SECONDS} сек.):",
        reply_markup=build_subscribe_keyboard(missing),
        parse_mode="HTML",
    )
    start_chat_watch_if_needed(bot, chat_id, uid)


async def send_question_msg(bot, chat_id, uid, q):
    opts = json.loads(q["options"])
    order = list(range(len(opts)))
    random.shuffle(order)
    perm = "".join(map(str, order))
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=opts[i], callback_data=f"a:{q['id']}:{p}:{perm}")]
            for p, i in enumerate(order)
        ]
    )
    scope_label = "🌍 Общий" if q["scope"] == "global" else "👤 Личный"
    db.execute("UPDATE sessions SET current_q_id=? WHERE user_id=?", (q["id"], uid))
    db.commit()
    await bot.send_message(
        chat_id,
        f"❓ <b>{q['question']}</b>\n<i>{scope_label} • 📦 Ящик Лейтнера: {q['box']}/5</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def send_next(bot, chat_id, uid):
    s = db.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
    if not s:
        return
    if s["remaining"] <= 0:
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        db.commit()
        pct = round(s["ok"] / s["total"] * 100) if s["total"] else 0
        return await bot.send_message(
            chat_id,
            f"🏁 Сессия завершена: <b>{s['ok']}/{s['total']}</b> ({pct}%)\n"
            f"⭐ Очки: <b>{get_user_score(uid)}</b> • 🔥 Серия: <b>{streak_now(uid)} дн.</b>",
            parse_mode="HTML",
        )

    recent_rows = db.execute(
        "SELECT id FROM questions WHERE user_id=? AND last_ts>=?", (uid, s["started"])
    ).fetchall()
    exclude = [r["id"] for r in recent_rows]

    q = pick_question(uid, s["mode"], exclude)
    if not q:
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        db.commit()
        return await bot.send_message(chat_id, "Здесь ничего нет")

    db.execute("UPDATE sessions SET remaining=remaining-1 WHERE user_id=?", (uid,))
    db.commit()
    await send_question_msg(bot, chat_id, uid, q)


async def start_session(bot, chat_id, uid, mode):
    ok_sub, missing = await check_user_subscriptions(bot, uid)
    if not ok_sub:
        return await prompt_subscribe_chat(bot, chat_id, uid, missing)

    ensure_user(uid)
    started = int(time.time())
    if mode == "manual":
        n = DAILY_LIMIT - day_count(uid)
        if n <= 0:
            return await bot.send_message(chat_id, f"Дневной лимит {DAILY_LIMIT} вопросов исчерпан.")
    elif mode == "wrong":
        n = db.execute(
            "SELECT COUNT(*) c FROM questions WHERE user_id=? AND seen=1 AND last_ok=0", (uid,)
        ).fetchone()["c"]
        if n == 0:
            return await bot.send_message(chat_id, "Здесь ничего нет")
    else:
        n = AUTO_COUNT

    if not pick_question(uid, mode):
        return await bot.send_message(chat_id, "Здесь ничего нет")

    db.execute(
        "INSERT OR REPLACE INTO sessions(user_id,current_q_id,remaining,total,ok,mode,started) VALUES(?,0,?,0,0,?,?)",
        (uid, n, mode, started),
    )
    db.commit()
    await send_next(bot, chat_id, uid)
@router.message(CommandStart())
async def cmd_start(m: Message, bot: Bot):
    uid = m.from_user.id
    args = (m.text or "").split(maxsplit=1)
    ref_by = int(args[1][4:]) if len(args) > 1 and args[1].startswith("ref_") and args[1][4:].isdigit() else None

    is_new = ensure_user(uid, m.from_user.full_name or "", m.from_user.username or "", ref_by)
    if is_new and ref_by and ref_by != uid:
        if db.execute("SELECT user_id FROM users WHERE user_id=?", (ref_by,)).fetchone():
            try:
                await bot.send_message(
                    ref_by,
                    f"🎉 По вашей реферальной ссылке присоединился <b>{m.from_user.full_name}</b>!\n"
                    f"Вам начислено <b>+{REFERRAL_BONUS_LIMIT}</b> к лимиту личных тестов.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    ok_sub, missing = await check_user_subscriptions(bot, uid)
    if not ok_sub:
        return await prompt_subscribe_chat(bot, m.chat.id, uid, missing)

    inline = None
    if WEBAPP_URL:
        inline = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✨ Открыть Mini App", web_app=WebAppInfo(url=WEBAPP_URL))]]
        )
    role_note = "\n👑 <b>Режим администратора активирован.</b>" if is_admin(uid) else ""
    await m.answer(
        f"Привет, <b>{m.from_user.first_name}</b>! 👋\n\n"
        "Тренажёр по кривой Эббингауза и системе Лейтнера.\n"
        "• Верный ответ: <b>+10 очков</b>\n"
        "• Ошибка: <b>-5 очков</b>"
        f"{role_note}",
        reply_markup=get_menu(),
        parse_mode="HTML",
    )
    if inline:
        await m.answer("Открыть мини-приложение:", reply_markup=inline)


@router.message(F.text == BTN_ADD)
async def btn_add(m: Message, bot: Bot):
    uid = m.from_user.id
    ok_sub, missing = await check_user_subscriptions(bot, uid)
    if not ok_sub:
        return await prompt_subscribe_chat(bot, m.chat.id, uid, missing)

    ensure_user(uid, m.from_user.full_name or "", m.from_user.username or "")
    me = await bot.get_me()
    ref_link = get_referral_link(me.username, uid)
    share_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📤 Отправить друзьям", url=get_share_url(me.username, uid))]]
    )

    if is_admin(uid):
        return await m.answer(
            "📥 <b>Добавление тестов (Администратор):</b>\n\n"
            "• Отправьте <b>.json файл</b> или <b>JSON-текст</b>.\n"
            "• Или отправьте <b>простой текст</b> (1-я строка — вопрос, далее варианты с новой строки).\n"
            f"<b>Формат JSON:</b>\n<pre>{JSON_FORMAT_EXAMPLE}</pre>",
            parse_mode="HTML",
        )

    await m.answer(
        f"📥 <b>Личные тесты:</b> <b>{personal_count(uid)}/{personal_limit(uid)}</b> "
        f"(база {BASE_PERSONAL_LIMIT}, +{REFERRAL_BONUS_LIMIT} за друга)\n\n"
        "1. <b>Простым текстом:</b> 1-я строка — вопрос, далее каждый вариант с Enter.\n"
        "2. <b>Файлом .json</b> или <b>JSON-текстом</b>.\n\n"
        f"<b>Формат JSON:</b>\n<pre>{JSON_FORMAT_EXAMPLE}</pre>\n\n"
        f"🔗 Ссылка:\n<code>{ref_link}</code>",
        reply_markup=share_kb,
        parse_mode="HTML",
    )


@router.message(F.document)
async def on_document(m: Message, bot: Bot):
    uid = m.from_user.id
    ok_sub, missing = await check_user_subscriptions(bot, uid)
    if not ok_sub:
        return await prompt_subscribe_chat(bot, m.chat.id, uid, missing)

    ensure_user(uid, m.from_user.full_name or "", m.from_user.username or "")
    doc = m.document
    if not (doc.file_name or "").lower().endswith(".json"):
        return await m.answer("Отправьте файл с расширением <b>.json</b>.", parse_mode="HTML")
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        return await m.answer("Файл слишком большой (максимум 5 МБ).")
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    try:
        data = json.loads(decode_bytes(buf.getvalue()))
        items, errors = parse_items(data)
    except Exception as e:
        return await m.answer(f"❌ Ошибка при чтении файла: {e}")

    res = add_questions_with_scope(uid, items, "personal")
    msg = (
        f"✅ Добавлено: <b>{res['added']}</b>\n"
        f"♻️ Дубликатов: <b>{res['dup']}</b>\n"
        f"📌 Лимит: <b>{personal_count(uid)}/{personal_limit(uid)}</b>"
    )
    if res["skipped_limit"]:
        me = await bot.get_me()
        msg += f"\n⛔ Пропущено по лимиту: <b>{res['skipped_limit']}</b>\n<code>{get_referral_link(me.username, uid)}</code>"
    if errors:
        msg += f"\n⚠️ Ошибок: <b>{len(errors)}</b>\n" + "\n".join(errors[:5])
    await m.answer(msg, parse_mode="HTML")


@router.message(F.text == BTN_STATS)
async def btn_stats(m: Message, bot: Bot):
    uid = m.from_user.id
    ok_sub, missing = await check_user_subscriptions(bot, uid)
    if not ok_sub:
        return await prompt_subscribe_chat(bot, m.chat.id, uid, missing)

    ensure_user(uid, m.from_user.full_name or "", m.from_user.username or "")
    me = await bot.get_me()
    now_ts = int(time.time())
    total = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=?", (uid,)).fetchone()["c"]
    due = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=? AND due<=?", (uid, now_ts)).fetchone()["c"]
    mastered = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=? AND box>=4", (uid,)).fetchone()["c"]
    u = db.execute("SELECT referral_count FROM users WHERE user_id=?", (uid,)).fetchone()
    refs = u["referral_count"] if u else 0
    share_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📤 Отправить друзьям", url=get_share_url(me.username, uid))]]
    )
    await m.answer(
        f"📊 <b>Статистика:</b>\n\n"
        f"⭐ Очки: <b>{get_user_score(uid)}</b> (+10 / -5)\n"
        f"📚 Всего вопросов: <b>{total}</b>\n"
        f"✍️ Личные тесты: <b>{personal_count(uid)}/{personal_limit(uid)}</b>\n"
        f"⏳ К повторению: <b>{due}</b>\n"
        f"🏆 Закреплено (4–5): <b>{mastered}</b>\n"
        f"🔥 Серия: <b>{streak_now(uid)} дн.</b>\n"
        f"👥 Друзей: <b>{refs}</b>\n"
        f"🔗 Ссылка:\n<code>{get_referral_link(me.username, uid)}</code>",
        reply_markup=share_kb,
        parse_mode="HTML",
    )


@router.message(F.text == BTN_WORK)
async def btn_work(m: Message, bot: Bot):
    await start_session(bot, m.chat.id, m.from_user.id, "manual")


@router.message(F.text == BTN_WRONG)
async def btn_wrong(m: Message, bot: Bot):
    await start_session(bot, m.chat.id, m.from_user.id, "wrong")


@router.message(F.text)
async def on_text_input(m: Message, bot: Bot):
    uid = m.from_user.id
    ok_sub, missing = await check_user_subscriptions(bot, uid)
    if not ok_sub:
        return await prompt_subscribe_chat(bot, m.chat.id, uid, missing)

    txt = (m.text or "").strip()
    if not txt:
        return
    ensure_user(uid, m.from_user.full_name or "", m.from_user.username or "")

    if txt.startswith("[") or txt.startswith("{"):
        try:
            data = json.loads(decode_bytes(txt.encode("utf-8")))
            items, errors = parse_items(data)
        except Exception as e:
            return await m.answer(f"❌ Некорректный JSON: {e}")
        res = add_questions_with_scope(uid, items, "personal")
        msg = (
            f"✅ Добавлено: <b>{res['added']}</b>\n"
            f"♻️ Дубликатов: <b>{res['dup']}</b>\n"
            f"📌 Лимит: <b>{personal_count(uid)}/{personal_limit(uid)}</b>"
        )
        if res["skipped_limit"]:
            me = await bot.get_me()
            msg += f"\n⛔ Пропущено по лимиту: <b>{res['skipped_limit']}</b>\n<code>{get_referral_link(me.username, uid)}</code>"
        if errors:
            msg += f"\n⚠️ Ошибок: <b>{len(errors)}</b>\n" + "\n".join(errors[:5])
        return await m.answer(msg, parse_mode="HTML")

    if "\n" in txt:
        if not is_admin(uid) and personal_remaining(uid) <= 0:
            me = await bot.get_me()
            return await m.answer(
                f"⛔ Достигнут лимит ({personal_limit(uid)} тестов).\n"
                f"Пригласите друга (+{REFERRAL_BONUS_LIMIT} тестов):\n<code>{get_referral_link(me.username, uid)}</code>",
                parse_mode="HTML",
            )
        try:
            question, options = parse_plain_lines(txt)
        except ValueError as e:
            return await m.answer(f"⚠️ {e}")

        draft_id = create_draft(uid, question, options, "personal")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=opt, callback_data=f"setans:{draft_id}:{idx}")]
                for idx, opt in enumerate(options)
            ]
        )
        return await m.answer(
            f"📝 <b>Предпросмотр теста:</b>\n❓ <b>{question}</b>\n\n"
            "Нажмите на <b>правильный вариант ответа</b>:",
            reply_markup=kb,
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("sublink:"))
async def on_sublink_click(cb: CallbackQuery, bot: Bot):
    start_chat_watch_if_needed(bot, cb.message.chat.id, cb.from_user.id)
    await cb.answer(f"Проверяем подписку каждую секунду ({SUB_WATCH_SECONDS} сек.)...")


@router.callback_query(F.data.startswith("setans:"))
async def on_set_answer(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    ok_sub, missing = await check_user_subscriptions(bot, uid)
    if not ok_sub:
        await prompt_subscribe_chat(bot, cb.message.chat.id, uid, missing)
        return await cb.answer("Сначала подпишитесь на канал", show_alert=True)

    _, draft_id, idx_s = cb.data.split(":")
    idx = int(idx_s)
    draft = db.execute("SELECT * FROM pending_drafts WHERE draft_id=? AND user_id=?", (draft_id, uid)).fetchone()
    if not draft:
        return await cb.answer("Черновик устарел", show_alert=True)

    options = json.loads(draft["options"])
    if idx < 0 or idx >= len(options):
        return await cb.answer("Неверный вариант", show_alert=True)

    res = add_questions_with_scope(uid, [(draft["question"], options, idx)], draft["scope"] or "personal")
    db.execute("DELETE FROM pending_drafts WHERE draft_id=?", (draft_id,))
    db.commit()
    try:
        await cb.message.delete()
    except Exception:
        pass

    if res["added"]:
        await cb.answer("✅ Сохранено!")
        await cb.message.answer(
            f"✅ <b>Вопрос добавлен!</b>\n❓ {draft['question']}\n✔️ Ответ: <b>{options[idx]}</b>",
            parse_mode="HTML",
        )
    elif res["skipped_limit"]:
        await cb.answer("⛔ Лимит исчерпан", show_alert=True)
    else:
        await cb.answer("♻️ Дубликат")


@router.callback_query(F.data.startswith("a:"))
async def on_answer(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    ok_sub, missing = await check_user_subscriptions(bot, uid)
    if not ok_sub:
        await prompt_subscribe_chat(bot, cb.message.chat.id, uid, missing)
        return await cb.answer("Сначала подпишитесь на канал", show_alert=True)

    _, qid_s, pos_s, perm = cb.data.split(":")
    qid, pos = int(qid_s), int(pos_s)
    sess = db.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
    if sess and sess["current_q_id"] != qid:
        return await cb.answer("На этот вопрос уже дан ответ!")

    q = db.execute("SELECT * FROM questions WHERE id=? AND user_id=?", (qid, uid)).fetchone()
    if not q:
        return await cb.answer("Вопрос не найден")

    opts = json.loads(q["options"])
    correct = q["correct"]
    ok = int(perm[pos]) == correct

    rows = []
    for p, ch in enumerate(perm):
        i = int(ch)
        mark = "🟢 " if i == correct else ("🔴 " if p == pos else "")
        rows.append([InlineKeyboardButton(text=mark + opts[i], callback_data="noop")])
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass

    res = apply_leitner_answer(uid, qid, ok)
    if sess:
        db.execute(
            "UPDATE sessions SET current_q_id=0, total=total+1, ok=ok+? WHERE user_id=?",
            (1 if ok else 0, uid),
        )
        if sess["mode"] == "manual":
            bump_day(uid)
        db.commit()

    pts = "+10" if ok else "-5"
    await cb.answer(f"{'✅ Верно!' if ok else '❌ Ошибка!'} ({pts}) 📦 {res['old_box']}→{res['new_box']}")
    await send_next(bot, cb.message.chat.id, uid)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


def verify_telegram_webapp(init_data: str) -> dict | None:
    if not init_data:
        return None
    parsed = dict(parse_qsl(init_data, strict_parsing=False))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None
    return json.loads(parsed.get("user", "{}"))


def get_api_user(request: web.Request) -> dict | None:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = verify_telegram_webapp(init_data)
    if user and "id" in user:
        full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        ensure_user(int(user["id"]), full_name, user.get("username", ""))
        return user
    return None
async def handle_index(request: web.Request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "webapp.html"))


async def api_dashboard(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Доступ запрещён"}, status=401)
    uid = int(user["id"])
    bot = request.app["bot"]
    bot_info = await bot.get_me()

    sub_ok, missing_channels = await check_user_subscriptions(bot, uid)
    all_channels = list_mandatory_channels()
    now_ts = int(time.time())

    total = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=?", (uid,)).fetchone()["c"]
    due = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=? AND due<=?", (uid, now_ts)).fetchone()["c"]
    wrong = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=? AND seen=1 AND last_ok=0", (uid,)).fetchone()["c"]
    agg = db.execute(
        "SELECT COALESCE(SUM(attempts),0) att, COALESCE(SUM(correct_count),0) cor FROM questions WHERE user_id=?",
        (uid,),
    ).fetchone()
    accuracy = round((agg["cor"] / agg["att"]) * 100) if agg["att"] > 0 else 0

    boxes = {str(i): 0 for i in range(6)}
    for r in db.execute("SELECT box, COUNT(*) c FROM questions WHERE user_id=? GROUP BY box", (uid,)).fetchall():
        boxes[str(r["box"])] = r["c"]

    u_row = db.execute("SELECT referral_count FROM users WHERE user_id=?", (uid,)).fetchone()
    refs = u_row["referral_count"] if u_row else 0

    return web.json_response({
        "subscription": {
            "ok": sub_ok,
            "missing_channels": missing_channels,
            "all_channels": all_channels,
            "watch_seconds": SUB_WATCH_SECONDS,
        },
        "user": {
            "id": uid,
            "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or f"Пользователь {uid}",
            "username": user.get("username", ""),
            "is_admin": is_admin(uid),
            "streak": streak_now(uid),
            "today_done": day_count(uid),
            "daily_limit": DAILY_LIMIT,
            "own_questions": personal_count(uid),
            "personal_limit": personal_limit(uid),
            "personal_remaining": personal_remaining(uid),
            "referral_count": refs,
            "referral_bonus": REFERRAL_BONUS_LIMIT,
            "referral_link": get_referral_link(bot_info.username, uid),
            "share_url": get_share_url(bot_info.username, uid),
            "score": get_user_score(uid),
        },
        "stats": {"total": total, "due": due, "wrong": wrong, "accuracy": accuracy, "attempts": agg["att"], "boxes": boxes},
        "json_example": JSON_FORMAT_EXAMPLE,
        "leaderboard": build_leaderboard(20),
        "admin_users": build_admin_users_list() if is_admin(uid) else [],
    })


async def api_sub_watch_start(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Доступ запрещён"}, status=401)
    uid = int(user["id"])
    now_ts = int(time.time())
    webapp_watch_state[uid] = {"started_ts": now_ts, "expires_ts": now_ts + SUB_WATCH_SECONDS}
    ok_sub, missing = await check_user_subscriptions(request.app["bot"], uid)
    return web.json_response({
        "ok": ok_sub,
        "missing_channels": missing,
        "started_ts": now_ts,
        "expires_ts": now_ts + SUB_WATCH_SECONDS,
    })


async def api_check_sub(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Доступ запрещён"}, status=401)
    uid = int(user["id"])
    now_ts = int(time.time())
    ok_sub, missing = await check_user_subscriptions(request.app["bot"], uid)
    st = webapp_watch_state.get(uid)
    timed_out = bool(not ok_sub and st and now_ts >= st["expires_ts"])
    return web.json_response({
        "ok": ok_sub,
        "missing_channels": missing,
        "timed_out": timed_out,
        "remaining_sec": max(0, st["expires_ts"] - now_ts) if st else 0,
    })


async def api_channels_save(request: web.Request):
    user = get_api_user(request)
    if not user or not is_admin(int(user["id"])):
        return web.json_response({"error": "Нет прав администратора"}, status=403)
    body = await request.json()
    ch_id = body.get("id")
    try:
        title, chat_ref, url = normalize_channel_input(body.get("chat_ref", ""), body.get("url", ""), body.get("title", ""))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    if ch_id:
        db.execute("UPDATE mandatory_channels SET title=?, chat_ref=?, url=? WHERE id=?", (title, chat_ref, url, int(ch_id)))
    else:
        db.execute("INSERT INTO mandatory_channels(title, chat_ref, url) VALUES(?,?,?)", (title, chat_ref, url))
    db.commit()
    return web.json_response({"ok": True, "channels": list_mandatory_channels()})


async def api_channels_delete(request: web.Request):
    user = get_api_user(request)
    if not user or not is_admin(int(user["id"])):
        return web.json_response({"error": "Нет прав администратора"}, status=403)
    body = await request.json()
    db.execute("DELETE FROM mandatory_channels WHERE id=?", (int(body.get("id", 0)),))
    db.commit()
    return web.json_response({"ok": True, "channels": list_mandatory_channels()})


async def api_next_question(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Доступ запрещён"}, status=401)
    uid = int(user["id"])
    ok_sub, missing = await check_user_subscriptions(request.app["bot"], uid)
    if not ok_sub:
        return web.json_response({"error": "Требуется подписка", "sub_required": True, "missing_channels": missing}, status=403)

    mode = request.query.get("mode", "due")
    exclude = [int(x) for x in request.query.get("exclude", "").split(",") if x.isdigit()]
    q = pick_question(uid, mode, exclude)
    if not q:
        return web.json_response({"question": None})

    indexed_opts = list(enumerate(json.loads(q["options"])))
    random.shuffle(indexed_opts)
    return web.json_response({
        "question": {
            "id": q["id"],
            "text": q["question"],
            "box": q["box"],
            "scope": q["scope"],
            "options": [{"idx": idx, "text": txt} for idx, txt in indexed_opts],
        }
    })


async def api_submit_answer(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Доступ запрещён"}, status=401)
    uid = int(user["id"])
    ok_sub, missing = await check_user_subscriptions(request.app["bot"], uid)
    if not ok_sub:
        return web.json_response({"error": "Требуется подписка", "sub_required": True, "missing_channels": missing}, status=403)

    body = await request.json()
    qid = int(body.get("question_id", 0))
    chosen_idx = int(body.get("option_idx", -1))
    q = db.execute("SELECT * FROM questions WHERE id=? AND user_id=?", (qid, uid)).fetchone()
    if not q:
        return web.json_response({"error": "Вопрос не найден"}, status=404)

    is_ok = chosen_idx == q["correct"]
    res = apply_leitner_answer(uid, qid, is_ok)
    bump_day(uid)
    return web.json_response({
        "is_ok": is_ok,
        "correct_idx": q["correct"],
        "old_box": res["old_box"],
        "new_box": res["new_box"],
        "delta": res["delta"],
        "streak": streak_now(uid),
        "score": res["score"],
    })


async def api_import_json(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Доступ запрещён"}, status=401)
    uid = int(user["id"])
    ok_sub, _ = await check_user_subscriptions(request.app["bot"], uid)
    if not ok_sub:
        return web.json_response({"error": "Требуется подписка", "sub_required": True}, status=403)

    body = await request.json()
    try:
        data = json.loads(decode_bytes((body.get("json_text", "")).encode("utf-8")))
        items, errors = parse_items(data)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

    res = add_questions_with_scope(uid, items, body.get("scope", "personal"))
    return web.json_response({
        **res,
        "errors_count": len(errors),
        "errors": errors[:5],
        "own_questions": personal_count(uid),
        "personal_limit": personal_limit(uid),
    })


async def api_add_plain(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Доступ запрещён"}, status=401)
    uid = int(user["id"])
    ok_sub, _ = await check_user_subscriptions(request.app["bot"], uid)
    if not ok_sub:
        return web.json_response({"error": "Требуется подписка", "sub_required": True}, status=403)

    body = await request.json()
    question = norm(body.get("question", ""))
    options = [norm(o) for o in body.get("options", []) if norm(o)]
    correct_idx = int(body.get("correct_idx", -1))
    if not question:
        return web.json_response({"error": "Введите текст вопроса"}, status=400)
    if not 2 <= len(options) <= 6:
        return web.json_response({"error": "Нужно от 2 до 6 вариантов ответа"}, status=400)
    if len({o.lower() for o in options}) < len(options):
        return web.json_response({"error": "Варианты ответа не должны повторяться"}, status=400)
    if not (0 <= correct_idx < len(options)):
        return web.json_response({"error": "Выберите правильный вариант ответа"}, status=400)

    res = add_questions_with_scope(uid, [(question, options, correct_idx)], body.get("scope", "personal"))
    return web.json_response({
        **res,
        "correct_text": options[correct_idx],
        "own_questions": personal_count(uid),
        "personal_limit": personal_limit(uid),
    })


async def api_admin_questions_list(request: web.Request):
    user = get_api_user(request)
    if not user or not is_admin(int(user["id"])):
        return web.json_response({"error": "Нет прав администратора"}, status=403)
    uid = int(user["id"])
    scope = request.query.get("scope", "personal")
    if scope not in ("personal", "global"):
        scope = "personal"

    if scope == "global":
        rows = db.execute(
            "SELECT id, h, question, options, correct, box FROM questions "
            "WHERE user_id=0 AND scope='global' ORDER BY id DESC"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, h, question, options, correct, box FROM questions "
            "WHERE user_id=? AND scope='personal' ORDER BY id DESC",
            (uid,),
        ).fetchall()

    items = []
    for r in rows:
        opts = json.loads(r["options"])
        ans_text = opts[r["correct"]] if 0 <= r["correct"] < len(opts) else ""
        items.append({
            "id": r["id"],
            "h": r["h"],
            "question": r["question"],
            "options": opts,
            "answer": ans_text,
            "box": r["box"],
            "scope": scope,
        })
    return web.json_response({"scope": scope, "questions": items})


async def api_admin_delete_question(request: web.Request):
    user = get_api_user(request)
    if not user or not is_admin(int(user["id"])):
        return web.json_response({"error": "Нет прав администратора"}, status=403)
    uid = int(user["id"])
    body = await request.json()
    scope = body.get("scope", "personal")
    qid = int(body.get("id", 0))

    if scope == "global":
        row = db.execute("SELECT h FROM questions WHERE id=? AND user_id=0 AND scope='global'", (qid,)).fetchone()
        if row:
            db.execute("DELETE FROM questions WHERE h=? AND scope='global'", (row["h"],))
    else:
        db.execute("DELETE FROM questions WHERE id=? AND user_id=? AND scope='personal'", (qid, uid))
    db.commit()
    return web.json_response({"ok": True})


async def scheduler(bot: Bot):
    while True:
        try:
            now = now_dt()
            if now.hour >= AUTO_HOUR:
                t = today()
                users = db.execute(
                    "SELECT user_id FROM users WHERE COALESCE(last_auto,'')<>? "
                    "AND EXISTS(SELECT 1 FROM questions WHERE questions.user_id=users.user_id)",
                    (t,),
                ).fetchall()
                for u in users:
                    uid = u["user_id"]
                    db.execute("UPDATE users SET last_auto=? WHERE user_id=?", (t, uid))
                    db.commit()
                    try:
                        await start_session(bot, uid, uid, "auto")
                    except Exception as e:
                        log.warning("Ошибка авто-теста для %s: %s", uid, e)
        except Exception as e:
            log.exception("Ошибка планировщика: %s", e)
        await asyncio.sleep(30)


async def main():
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/dashboard", api_dashboard)
    app.router.add_post("/api/sub-watch/start", api_sub_watch_start)
    app.router.add_get("/api/check-sub", api_check_sub)
    app.router.add_post("/api/channels/save", api_channels_save)
    app.router.add_post("/api/channels/delete", api_channels_delete)
    app.router.add_get("/api/question", api_next_question)
    app.router.add_post("/api/answer", api_submit_answer)
    app.router.add_post("/api/import", api_import_json)
    app.router.add_post("/api/add-plain", api_add_plain)
    app.router.add_get("/api/admin/questions", api_admin_questions_list)
    app.router.add_post("/api/admin/questions/delete", api_admin_delete_question)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Сервер Mini App запущен на порту %s", PORT)

    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
