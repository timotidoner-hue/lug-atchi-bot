import asyncio
import hashlib
import io
import json
import logging
import os
import random
import sqlite3
import re
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("testbot")

# ---------------- Sozlamalar ----------------
TOKEN = os.environ["BOT_TOKEN"]
DATA_DIR = os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "."  # Railway Volume avtomatik aniqlanadi
TZ = ZoneInfo(os.getenv("BOT_TZ", "Asia/Tashkent"))
AUTO_HOUR = 16        # avtomatik test vaqti (soat)
AUTO_COUNT = 10       # kunlik avtomatik testlar soni
DAILY_LIMIT = 50      # "Test ishlash" tugmasi bilan kuniga maksimum
REPEAT_DAYS = 2       # to'g'ri ishlangan test necha kundan keyin qaytadi
WRONG_REPEAT_DAYS = 1  # xato ishlangan test necha kundan keyin qaytadi

BTN_ADD = "➕ Test qo'shish"
BTN_COUNT = "📊 Testlar soni"
BTN_PROGRESS = "📈 Progress"
BTN_WORK = "📝 Test ishlash"

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_WORK), KeyboardButton(text=BTN_PROGRESS)],
        [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_COUNT)],
    ],
    resize_keyboard=True,
)

# ---------------- Baza ----------------
os.makedirs(DATA_DIR, exist_ok=True)
db = sqlite3.connect(os.path.join(DATA_DIR, "bot.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
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
    attempts INTEGER DEFAULT 0,
    seen INTEGER DEFAULT 0,
    last_ok INTEGER DEFAULT 0,
    due INTEGER DEFAULT 0,
    UNIQUE(user_id, h)
);
CREATE TABLE IF NOT EXISTS sessions(
    user_id INTEGER PRIMARY KEY,
    remaining INTEGER,
    total INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 0,
    mode TEXT
);
"""
)
db.commit()


def now_dt():
    return datetime.now(TZ)


def today():
    return now_dt().date().isoformat()


def ensure_user(uid):
    if not db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
        la = today() if now_dt().hour >= AUTO_HOUR else None
        db.execute("INSERT INTO users(user_id,last_auto) VALUES(?,?)", (uid, la))
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


# ---------------- JSON parsing ----------------
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
CYR_LETTERS = "АБВГДЕ"  # тоҷикӣ/русӣ тартиби алифбо


def norm(s):
    """Unicode NFC + ortiqcha bo'shliqlarni tozalash (tojik harflari ҳ, ҷ, қ, ӯ, ӣ, ғ uchun)."""
    s = unicodedata.normalize("NFC", str(s)).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


def decode_bytes(raw):
    for enc in ("utf-8-sig", "utf-16", "cp1251"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError("fayl kodirovkasi noma'lum (UTF-8 ishlating)")


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


def add_questions(uid, items):
    added = dup = 0
    for q, opts, correct in items:
        h = hashlib.sha1(q.lower().encode()).hexdigest()
        try:
            db.execute(
                "INSERT INTO questions(user_id,h,question,options,correct) VALUES(?,?,?,?,?)",
                (uid, h, q, json.dumps(opts, ensure_ascii=False), correct),
            )
            added += 1
        except sqlite3.IntegrityError:
            dup += 1
    db.commit()
    return added, dup


# ---------------- Test sessiyasi ----------------
def pick_question(uid):
    return db.execute(
        "SELECT * FROM questions WHERE user_id=? AND due<=? ORDER BY seen ASC, RANDOM() LIMIT 1",
        (uid, int(time.time())),
    ).fetchone()


async def send_question(bot, chat_id, q):
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
    await bot.send_message(chat_id, f"❓ {q['question']}", reply_markup=kb)


async def finish_session(bot, chat_id, uid, note=None):
    s = db.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
    db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    db.commit()
    if s and s["total"]:
        pct = round(s["ok"] / s["total"] * 100)
        await bot.send_message(
            chat_id,
            f"🏁 Sessiya tugadi: {s['ok']}/{s['total']} to'g'ri ({pct}%)\n🔥 Kun seriyasi: {streak_now(uid)}",
        )
    elif note:
        await bot.send_message(chat_id, note)


async def send_next(bot, chat_id, uid):
    s = db.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
    if not s:
        return
    if s["remaining"] <= 0:
        return await finish_session(bot, chat_id, uid)
    q = pick_question(uid)
    if not q:
        return await finish_session(bot, chat_id, uid, "Hozircha takrorlanadigan test qolmadi 🎉")
    await send_question(bot, chat_id, q)
    db.execute("UPDATE sessions SET remaining=remaining-1 WHERE user_id=?", (uid,))
    db.commit()
    if s["mode"] == "manual":
        bump_day(uid)


async def start_session(bot, chat_id, uid, mode):
    ensure_user(uid)
    if mode == "manual":
        n = DAILY_LIMIT - day_count(uid)
        if n <= 0:
            return await bot.send_message(chat_id, f"Bugungi {DAILY_LIMIT} ta limit tugadi. Ertaga davom etamiz 💪")
    else:
        n = AUTO_COUNT
    if not pick_question(uid):
        if mode == "manual":
            total = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=?", (uid,)).fetchone()["c"]
            msg = "Avval test qo'shing: ➕ Test qo'shish." if total == 0 else "Hozircha takrorlanadigan test yo'q. Keyinroq urinib ko'ring."
            await bot.send_message(chat_id, msg)
        return
    db.execute(
        "INSERT OR REPLACE INTO sessions(user_id,remaining,total,ok,mode) VALUES(?,?,0,0,?)",
        (uid, n, mode),
    )
    db.commit()
    if mode == "auto":
        await bot.send_message(chat_id, f"⏰ Kunlik {AUTO_COUNT} ta test boshlandi!")
    await send_next(bot, chat_id, uid)


# ---------------- Progress rasmi ----------------
def font(size):
    for p in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def battery_png(pct, done, total):
    col = (220, 53, 69) if pct < 34 else (255, 159, 28) if pct < 67 else (40, 180, 99)
    dark = (40, 44, 52)
    img = Image.new("RGB", (1000, 640), (245, 247, 250))
    d = ImageDraw.Draw(img)
    d.text((500, 60), "Progress", font=font(52), fill=dark, anchor="mm")
    d.rounded_rectangle((100, 130, 820, 380), radius=40, outline=dark, width=12)
    d.rounded_rectangle((832, 200, 884, 310), radius=14, fill=dark)
    x0, x1, y0, y1 = 130, 790, 160, 350
    fw = int((x1 - x0) * pct / 100)
    if fw > 0:
        d.rounded_rectangle((x0, y0, x0 + max(fw, 24), y1), radius=20, fill=col)
    d.text((500, 480), f"{pct:.0f}%", font=font(120), fill=col, anchor="mm")
    d.text((500, 580), f"{done} / {total} ta o'rganildi", font=font(44), fill=(100, 106, 118), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------- Handlers ----------------
router = Router()


@router.message(CommandStart())
async def cmd_start(m: Message):
    ensure_user(m.from_user.id)
    await m.answer(
        "Salom! 👋 Test botiga xush kelibsiz.\n\n"
        "➕ Test qo'shish — .json fayl yuboring\n"
        "📝 Test ishlash — kuniga 50 tagacha\n"
        f"⏰ Har kuni soat {AUTO_HOUR}:00 da {AUTO_COUNT} ta test avtomatik keladi.",
        reply_markup=MENU,
    )


@router.message(F.text == BTN_ADD)
async def btn_add(m: Message):
    ensure_user(m.from_user.id)
    await m.answer(
        "📎 .json faylni shu yerga yuboring.\n\n"
        "Format:\n"
        '[{"question": "Apple?", "options": ["Olma", "Nok", "Uzum", "Anor"], "answer": "Olma"}]\n\n'
        "answer: variant matni, harf (A–D) yoki raqam (1–4) bo'lishi mumkin."
    )


@router.message(F.document)
async def on_document(m: Message, bot: Bot):
    uid = m.from_user.id
    ensure_user(uid)
    doc = m.document
    if not (doc.file_name or "").lower().endswith(".json"):
        return await m.answer("Iltimos, .json fayl yuboring.")
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        return await m.answer("Fayl juda katta (5 MB gacha).")
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    try:
        data = json.loads(decode_bytes(buf.getvalue()))
        items, errors = parse_items(data)
    except Exception as e:
        return await m.answer(f"❌ Faylni o'qib bo'lmadi: {e}")
    added, dup = add_questions(uid, items)
    text = f"✅ Qo'shildi: {added}\n♻️ Takroriy (o'tkazib yuborildi): {dup}"
    if errors:
        text += f"\n⚠️ Xato savollar: {len(errors)}\n" + "\n".join(errors[:5])
    await m.answer(text)


@router.message(F.text == BTN_COUNT)
async def btn_count(m: Message):
    uid = m.from_user.id
    ensure_user(uid)
    total = db.execute("SELECT COUNT(*) c FROM questions WHERE user_id=?", (uid,)).fetchone()["c"]
    due = db.execute(
        "SELECT COUNT(*) c FROM questions WHERE user_id=? AND due<=?", (uid, int(time.time()))
    ).fetchone()["c"]
    await m.answer(
        f"📊 Jami testlar: {total}\n"
        f"🔁 Hozir ishlash mumkin: {due}\n"
        f"📅 Bugun ishlangan (tugma orqali): {day_count(uid)}/{DAILY_LIMIT}\n"
        f"🔥 Kun seriyasi: {streak_now(uid)}"
    )


@router.message(F.text == BTN_PROGRESS)
async def btn_progress(m: Message):
    uid = m.from_user.id
    ensure_user(uid)
    r = db.execute(
        "SELECT COUNT(*) t, COALESCE(SUM(last_ok),0) ok FROM questions WHERE user_id=?", (uid,)
    ).fetchone()
    total, done = r["t"], r["ok"]
    if total == 0:
        return await m.answer("Hali test yo'q. Avval ➕ Test qo'shish.")
    pct = done / total * 100
    await m.answer_photo(
        BufferedInputFile(battery_png(pct, done, total), "progress.png"),
        caption=f"📈 {pct:.0f}% — {done}/{total} ta test o'rganildi\n🔥 Kun seriyasi: {streak_now(uid)}",
    )


@router.message(F.text == BTN_WORK)
async def btn_work(m: Message, bot: Bot):
    await start_session(bot, m.chat.id, m.from_user.id, "manual")


@router.callback_query(F.data == "n")
async def noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("a:"))
async def on_answer(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    _, qid, pos, perm = cb.data.split(":")
    q = db.execute("SELECT * FROM questions WHERE id=? AND user_id=?", (int(qid), uid)).fetchone()
    if not q:
        return await cb.answer("Savol topilmadi")
    opts = json.loads(q["options"])
    pos = int(pos)
    correct = q["correct"]
    ok = int(perm[pos]) == correct

    rows = []
    for p, ch in enumerate(perm):
        i = int(ch)
        mark = "🟢 " if i == correct else ("🔴 " if p == pos else "")
        rows.append([InlineKeyboardButton(text=mark + opts[i], callback_data="n")])
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass

    days = REPEAT_DAYS if ok else WRONG_REPEAT_DAYS
    db.execute(
        "UPDATE questions SET attempts=attempts+1, seen=1, last_ok=?, due=? WHERE id=?",
        (1 if ok else 0, int(time.time()) + days * 86400, q["id"]),
    )
    db.execute(
        "UPDATE sessions SET total=total+1, ok=ok+? WHERE user_id=?", (1 if ok else 0, uid)
    )
    db.commit()
    touch_streak(uid)
    await cb.answer("✅ To'g'ri!" if ok else "❌ Xato")
    await send_next(bot, cb.message.chat.id, uid)


# ---------------- Avtomatik kunlik test ----------------
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
                        log.warning("auto test failed for %s: %s", uid, e)
        except Exception as e:
            log.exception("scheduler error: %s", e)
        await asyncio.sleep(30)


async def main():
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
