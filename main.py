import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("leitner_bot")

# ---------------- Sozlamalar ----------------
TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")  # Masalan: https://sizning-app.up.railway.app
PORT = int(os.getenv("PORT", 8080))
DATA_DIR = (
    os.getenv("DATA_DIR")
    or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    or ("/data" if os.path.isdir("/data") else ".")
)
TZ = ZoneInfo(os.getenv("BOT_TZ", "Asia/Tashkent"))
AUTO_HOUR = 16
AUTO_COUNT = 10
DAILY_LIMIT = 50

# Ebbinghaus + Leitner intervallari (soniyalarda)
# Box 0: 10 daqiqa, Box 1: 1 kun, Box 2: 3 kun, Box 3: 7 kun, Box 4: 16 kun, Box 5: 35 kun
LEITNER_INTERVALS = {
    0: 600,
    1: 86400,
    2: 3 * 86400,
    3: 7 * 86400,
    4: 16 * 86400,
    5: 35 * 86400,
}

# ---------------- Baza (WAL rejimi bilan) ----------------
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
    streak INTEGER DEFAULT 0,
    last_active TEXT,
    day TEXT,
    day_count INTEGER DEFAULT 0,
    last_auto TEXT
);
CREATE TABLE IF NOT EXISTS questions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
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
    UNIQUE(user_id, h)
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
"""
)

# Eski bazadan migratsiya qilish
for _sql in (
    "ALTER TABLE users ADD COLUMN full_name TEXT DEFAULT ''",
    "ALTER TABLE questions ADD COLUMN box INTEGER DEFAULT 0",
    "ALTER TABLE questions ADD COLUMN correct_count INTEGER DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN current_q_id INTEGER DEFAULT 0",
):
    try:
        db.execute(_sql)
    except sqlite3.OperationalError:
        pass
db.commit()


def now_dt():
    return datetime.now(TZ)


def today():
    return now_dt().date().isoformat()


def ensure_user(uid, full_name=""):
    row = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        la = today() if now_dt().hour >= AUTO_HOUR else None
        db.execute(
            "INSERT INTO users(user_id, full_name, last_auto) VALUES(?,?,?)",
            (uid, full_name, la),
        )
        db.commit()
    elif full_name and row["full_name"] != full_name:
        db.execute("UPDATE users SET full_name=? WHERE user_id=?", (full_name, uid))
        db.commit()


def touch_streak(uid):
    u = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    t = today()
    if u["last_active"] == t:
        return
    y = (now_dt().date() - timedelta(days=1)).isoformat()
    streak = u["streak"] + 1 if u["last_active"] == y else 1
    db.execute("UPDATE users SET streak=?, last_active=? WHERE user_id=?", (streak, t, uid))
    db.commit()


def streak_now(uid):
    u = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not u or not u["last_active"]:
        return 0
    y = (now_dt().date() - timedelta(days=1)).isoformat()
    return u["streak"] if u["last_active"] in (today(), y) else 0


def day_count(uid):
    u = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    return u["day_count"] if u and u["day"] == today() else 0


def bump_day(uid):
    c = day_count(uid) + 1
    db.execute("UPDATE users SET day=?, day_count=? WHERE user_id=?", (today(), c, uid))
    db.commit()


# ---------------- JSON Parsing ----------------
LIST_KEYS = ("questions", "tests", "savollar", "data", "саволҳо", "саволхо", "тестҳо", "тестхо")
Q_KEYS = ("question", "savol", "q", "text", "савол", "суол", "матн")
O_KEYS = (
    "options", "variants", "choices", "javoblar", "variantlar", "answers",
    "вариантҳо", "вариантхо", "варианты", "ҷавобҳо", "чавобхо", "ҷавобхо", "интихобҳо",
)
A_KEYS = (
    "answer", "correct", "togri", "to'g'ri", "correct_answer", "javob", "togri_javob",
    "ҷавоб", "чавоб", "дуруст", "ҷавоби_дуруст", "чавоби_дуруст", "ҷавоби дуруст", "рост",
)
CYR_LETTERS = "АБВГДЕ"


def norm(s):
    s = unicodedata.normalize("NFC", str(s)).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


def decode_bytes(raw):
    for enc in ("utf-8-sig", "utf-16", "cp1251"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError("Fayl kodirovkasi noma'lum (UTF-8 ishlating)")


def pick(d, keys):
    low = {norm(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in low:
            return low[k.lower()]
    return None


def find_correct(opts, letters, ans):
    n = len(opts)
    if ans is None or isinstance(ans, bool):
        raise ValueError("to'g'ri javob yo'q")
    if isinstance(ans, int):
        if 1 <= ans <= n:
            return ans - 1
        raise ValueError("to'g'ri javob raqami noto'g'ri")
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
    raise ValueError("to'g'ri javob aniqlanmadi")


def parse_one(it):
    if not isinstance(it, dict):
        raise ValueError("obyekt emas")
    q = pick(it, Q_KEYS)
    raw = pick(it, O_KEYS)
    ans = pick(it, A_KEYS)
    if not isinstance(q, str) or not q.strip():
        raise ValueError("savol topilmadi")
    letters = None
    if isinstance(raw, dict):
        letters = [norm(k).upper() for k in raw]
        opts = [norm(v) for v in raw.values()]
    elif isinstance(raw, list):
        opts = [norm(v) for v in raw]
    else:
        raise ValueError("variantlar topilmadi")
    if not 2 <= len(opts) <= 6 or any(not o for o in opts):
        raise ValueError("variantlar soni/matni noto'g'ri")
    if len({o.lower() for o in opts}) < len(opts):
        raise ValueError("variantlar takrorlangan")
    return norm(q), opts, find_correct(opts, letters, ans)


def parse_items(data):
    if isinstance(data, dict):
        lst = pick(data, LIST_KEYS)
        data = lst if isinstance(lst, list) else [data]
    if not isinstance(data, list):
        raise ValueError("JSON ro'yxat bo'lishi kerak")
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


def add_questions(uid, items):
    added = dup = 0
    for q, opts, correct in items:
        h = qhash(q)
        try:
            db.execute(
                "INSERT INTO questions(user_id,h,question,options,correct,box,due) VALUES(?,?,?,?,?,0,0)",
                (uid, h, q, json.dumps(opts, ensure_ascii=False), correct),
            )
            added += 1
        except sqlite3.IntegrityError:
            dup += 1
    db.commit()
    return added, dup


# ---------------- Ebbinghaus + Leitner Core Logikasi ----------------
def apply_leitner_answer(uid, qid, is_ok):
    q = db.execute("SELECT * FROM questions WHERE id=? AND user_id=?", (qid, uid)).fetchone()
    if not q:
        return None
    now_ts = int(time.time())
    old_box = q["box"] or 0
    if is_ok:
        new_box = min(old_box + 1, 5)
    else:
        new_box = 0  # Ebbinghaus reset: xato qilinsa 0-qutiga qaytadi

    interval = LEITNER_INTERVALS[new_box]
    next_due = now_ts + interval

    db.execute(
        """UPDATE questions SET 
           box=?, attempts=attempts+1, correct_count=correct_count+?, 
           seen=1, last_ok=?, due=?, last_ts=? 
           WHERE id=?""",
        (new_box, 1 if is_ok else 0, 1 if is_ok else 0, next_due, now_ts, qid),
    )
    db.commit()
    touch_streak(uid)
    return {"old_box": old_box, "new_box": new_box, "next_due": next_due}


def pick_question(uid, mode="due", exclude_ids=None):
    exclude_ids = exclude_ids or []
    placeholders = ",".join("?" * len(exclude_ids)) if exclude_ids else "0"
    now_ts = int(time.time())

    if mode == "wrong":
        query = f"""
            SELECT * FROM questions 
            WHERE user_id=? AND seen=1 AND last_ok=0 AND id NOT IN ({placeholders})
            ORDER BY last_ts ASC, RANDOM() LIMIT 1
        """
        return db.execute(query, (uid, *exclude_ids)).fetchone()

    # Avval muddati kelgan (due <= now) va eng kichik qutidagi (box ASC) savollar olinadi
    query = f"""
        SELECT * FROM questions 
        WHERE user_id=? AND due<=? AND id NOT IN ({placeholders})
        ORDER BY box ASC, seen ASC, RANDOM() LIMIT 1
    """
    return db.execute(query, (uid, now_ts, *exclude_ids)).fetchone()


# ---------------- Bot Klaviaturasi ----------------
BTN_WORK = "📝 Test ishlash"
BTN_WRONG = "❌ Xatolarni ishlash"
BTN_ADD = "➕ Test qo'shish"
BTN_STATS = "📊 Statistika"


def get_menu():
    rows = [
        [KeyboardButton(text=BTN_WORK), KeyboardButton(text=BTN_WRONG)],
        [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_STATS)],
    ]
    if WEBAPP_URL:
        rows.insert(
            0,
            [KeyboardButton(text="🚀 Mini App (Profil & Leitner)", web_app=WebAppInfo(url=WEBAPP_URL))],
        )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ---------------- Telegram Bot Handlers ----------------
router = Router()


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
    db.execute("UPDATE sessions SET current_q_id=? WHERE user_id=?", (q["id"], uid))
    db.commit()
    box_badge = f"📦 Quti {q['box']}/5"
    await bot.send_message(chat_id, f"❓ <b>{q['question']}</b>\n<i>{box_badge}</i>", reply_markup=kb, parse_mode="HTML")


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
            f"🏁 Sessiya tugadi: <b>{s['ok']}/{s['total']}</b> ({pct}%)\n🔥 Kun seriyasi: <b>{streak_now(uid)}</b>",
            parse_mode="HTML",
        )

    # Shu sessiya davomida ishlangan savollarni qaytarmaslik uchun
    recent_rows = db.execute(
        "SELECT id FROM questions WHERE user_id=? AND last_ts>=?", (uid, s["started"])
    ).fetchall()
    exclude = [r["id"] for r in recent_rows]

    q = pick_question(uid, s["mode"], exclude)
    if not q:
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        db.commit()
        return await bot.send_message(chat_id, "🎉 Hozircha takrorlanadigan savollar qolmadi!")

    db.execute("UPDATE sessions SET remaining=remaining-1 WHERE user_id=?", (uid,))
    db.commit()
    await send_question_msg(bot, chat_id, uid, q)


async def start_session(bot, chat_id, uid, mode):
    ensure_user(uid)
    started = int(time.time())
    if mode == "manual":
        n = DAILY_LIMIT - day_count(uid)
        if n <= 0:
            return await bot.send_message(chat_id, f"Bugungi {DAILY_LIMIT} ta limit tugadi. Ertaga davom etamiz 💪")
    elif mode == "wrong":
        n = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=? AND seen=1 AND last_ok=0", (uid,)).fetchone()["c"]
        if n == 0:
            return await bot.send_message(chat_id, "Xato qilingan savollar yo'q 🎉")
    else:
        n = AUTO_COUNT

    if not pick_question(uid, mode):
        return await bot.send_message(chat_id, "Hozircha takrorlash vaqti kelgan savollar yo'q. Keyinroq kiring!")

    db.execute(
        "INSERT OR REPLACE INTO sessions(user_id,current_q_id,remaining,total,ok,mode,started) VALUES(?,0,?,0,0,?,?)",
        (uid, n, mode, started),
    )
    db.commit()
    await send_next(bot, chat_id, uid)


@router.message(CommandStart())
async def cmd_start(m: Message):
    ensure_user(m.from_user.id, m.from_user.full_name)
    inline = None
    if WEBAPP_URL:
        inline = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✨ Mini App ni ochish", web_app=WebAppInfo(url=WEBAPP_URL))]]
        )
    await m.answer(
        f"Salom, <b>{m.from_user.first_name}</b>! 👋\n\n"
        "Bu bot <b>Ebbinghaus xotira egri chizig'i</b> va <b>Leitner qutilari</b> asosida so'z va testlarni abadiy yodlashga yordam beradi.\n\n"
        "🚀 To'liq vizual statistika, profil va qulay test ishlash uchun <b>Mini App</b> tugmasini bosing!",
        reply_markup=get_menu(),
        parse_mode="HTML",
    )
    if inline:
        await m.answer("Tezkor kirish:", reply_markup=inline)


@router.message(F.text == BTN_ADD)
async def btn_add(m: Message):
    await m.answer(
        "📎 <b>.json</b> fayl yuboring yoki Mini App orqali matn ko'rinishida qo'shing.\n\n"
        "<code>[{\"question\": \"Abandon\", \"options\": [\"Tark etmoq\", \"Qabul qilmoq\", \"Yugurmoq\", \"O'ylamoq\"], \"answer\": \"Tark etmoq\"}]</code>",
        parse_mode="HTML",
    )


@router.message(F.document)
async def on_document(m: Message, bot: Bot):
    uid = m.from_user.id
    ensure_user(uid, m.from_user.full_name)
    doc = m.document
    if not (doc.file_name or "").lower().endswith(".json"):
        return await m.answer("Iltimos, .json fayl yuboring.")
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    try:
        data = json.loads(decode_bytes(buf.getvalue()))
        items, errors = parse_items(data)
    except Exception as e:
        return await m.answer(f"❌ Faylni o'qib bo'lmadi: {e}")
    added, dup = add_questions(uid, items)
    await m.answer(f"✅ Qo'shildi: <b>{added}</b>\n♻️ Takroriy: <b>{dup}</b>\n⚠️ Xato: <b>{len(errors)}</b>", parse_mode="HTML")


@router.message(F.text == BTN_STATS)
async def btn_stats(m: Message):
    uid = m.from_user.id
    ensure_user(uid, m.from_user.full_name)
    now_ts = int(time.time())
    total = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=?", (uid,)).fetchone()["c"]
    due = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=? AND due<=?", (uid, now_ts)).fetchone()["c"]
    mastered = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=? AND box>=4", (uid,)).fetchone()["c"]
    await m.answer(
        f"📊 <b>Statistika:</b>\n\n"
        f"📚 Jami savollar: <b>{total}</b>\n"
        f"⏳ Takrorlash vaqti kelgan: <b>{due}</b>\n"
        f"🏆 Mustahkam yodlangan (Box 4-5): <b>{mastered}</b>\n"
        f"🔥 Kun seriyasi: <b>{streak_now(uid)}</b>",
        parse_mode="HTML",
    )


@router.message(F.text == BTN_WORK)
async def btn_work(m: Message, bot: Bot):
    await start_session(bot, m.chat.id, m.from_user.id, "manual")


@router.message(F.text == BTN_WRONG)
async def btn_wrong(m: Message, bot: Bot):
    await start_session(bot, m.chat.id, m.from_user.id, "wrong")


@router.callback_query(F.data.startswith("a:"))
async def on_answer(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    _, qid_s, pos_s, perm = cb.data.split(":")
    qid, pos = int(qid_s), int(pos_s)

    # Double-click himoyasi
    sess = db.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
    if sess and sess["current_q_id"] != qid:
        return await cb.answer("Bu savolga allaqachon javob berilgan!")

    q = db.execute("SELECT * FROM questions WHERE id=? AND user_id=?", (qid, uid)).fetchone()
    if not q:
        return await cb.answer("Savol topilmadi")

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

    box_msg = f"📦 Quti: {res['old_box']} ➔ {res['new_box']}" if res else ""
    await cb.answer(f"{'✅ To\'g\'ri!' if ok else '❌ Xato!'} {box_msg}")
    await send_next(bot, cb.message.chat.id, uid)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


# ---------------- Mini App Xavfsizlik va REST API ----------------
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
        ensure_user(user["id"], full_name)
        return user
    return None


async def handle_index(request: web.Request):
    html_path = os.path.join(os.path.dirname(__file__), "webapp.html")
    return web.FileResponse(html_path)


async def api_dashboard(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Unauthorized"}, status=401)
    uid = user["id"]
    now_ts = int(time.time())

    total = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=?", (uid,)).fetchone()["c"]
    due = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=? AND due<=?", (uid, now_ts)).fetchone()["c"]
    wrong = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id         "=? AND seen=1 AND last_ok=0", (uid,)
    ).fetchone()["c"]

    agg = db.execute(
        "SELECT COALESCE(SUM(attempts),0) att, COALESCE(SUM(correct_count),0) cor FROM questions WHERE user_id=?",
        (uid,),
    ).fetchone()
    accuracy = round((agg["cor"] / agg["att"]) * 100) if agg["att"] > 0 else 0

    boxes = {str(i): 0 for i in range(6)}
    for r in db.execute(
        "SELECT box, COUNT(*) c FROM questions WHERE user_id=? GROUP BY box", (uid,)
    ).fetchall():
        boxes[str(r["box"])] = r["c"]

    return web.json_response({
        "user": {
            "id": uid,
            "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "username": user.get("username", ""),
            "streak": streak_now(uid),
            "today_done": day_count(uid),
            "daily_limit": DAILY_LIMIT,
        },
        "stats": {
            "total": total,
            "due": due,
            "wrong": wrong,
            "accuracy": accuracy,
            "attempts": agg["att"],
            "boxes": boxes,
        },
    })


async def api_next_question(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Unauthorized"}, status=401)
    uid = user["id"]
    mode = request.query.get("mode", "due")
    exclude_raw = request.query.get("exclude", "")
    exclude = [int(x) for x in exclude_raw.split(",") if x.isdigit()]

    q = pick_question(uid, mode, exclude)
    if not q:
        return web.json_response({"question": None})

    opts = json.loads(q["options"])
    indexed_opts = list(enumerate(opts))
    random.shuffle(indexed_opts)

    return web.json_response({
        "question": {
            "id": q["id"],
            "text": q["question"],
            "box": q["box"],
            "options": [{"idx": idx, "text": txt} for idx, txt in indexed_opts],
        }
    })


async def api_submit_answer(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Unauthorized"}, status=401)
    uid = user["id"]
    body = await request.json()
    qid = int(body.get("question_id", 0))
    chosen_idx = int(body.get("option_idx", -1))

    q = db.execute("SELECT * FROM questions WHERE id=? AND user_id=?", (qid, uid)).fetchone()
    if not q:
        return web.json_response({"error": "Not found"}, status=404)

    is_ok = chosen_idx == q["correct"]
    res = apply_leitner_answer(uid, qid, is_ok)
    bump_day(uid)

    return web.json_response({
        "is_ok": is_ok,
        "correct_idx": q["correct"],
        "old_box": res["old_box"],
        "new_box": res["new_box"],
        "streak": streak_now(uid),
    })


async def api_import_json(request: web.Request):
    user = get_api_user(request)
    if not user:
        return web.json_response({"error": "Unauthorized"}, status=401)
    uid = user["id"]
    body = await request.json()
    raw_text = body.get("json_text", "")
    try:
        data = json.loads(raw_text)
        items, errors = parse_items(data)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

    added, dup = add_questions(uid, items)
    return web.json_response({
        "added": added,
        "dup": dup,
        "errors_count": len(errors),
        "errors": errors[:5],
    })


# ---------------- Avtomatik Scheduler & Main ----------------
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
                        log.warning("Auto test failed for %s: %s", uid, e)
        except Exception as e:
            log.exception("Scheduler error: %s", e)
        await asyncio.sleep(30)


async def main():
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/dashboard", api_dashboard)
    app.router.add_get("/api/question", api_next_question)
    app.router.add_post("/api/answer", api_submit_answer)
    app.router.add_post("/api/import", api_import_json)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("WebApp server running on port %s", PORT)

    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
