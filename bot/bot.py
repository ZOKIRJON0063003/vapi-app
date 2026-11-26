"""
Sabah AI bot: mijoz Telegram'da tugmani bosadi va Vapi assistant bilan ovozli gaplashadi.
Hech narsa so'ramaydi: .env fayldan kalitlarni o'zi topadi va tekshiradi.
Ishga tushirish:  cd ~/vapi-bot && source venv/bin/activate && python bot.py
"""
import os
import re
import functools
print = functools.partial(print, flush=True)  # log faylga darhol yozilsin
import sys
import logging
from urllib.parse import urlencode
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # token loglarga chiqmasin
log = logging.getLogger("vapi-bot")

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")
WEBAPP_URL = "https://zokirjon0063003.github.io/vapi-app/"
VAPI = "https://api.vapi.ai"
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def read_env():
    """.env ni xavfsiz o'qiydi (ichidagi buyruqlarni bajarmaydi)."""
    pairs, text = [], ""
    if os.path.exists(ENV_FILE):
        text = open(ENV_FILE, encoding="utf-8", errors="ignore").read()
        for line in text.splitlines():
            m = re.match(r"^\s*([A-Z_]+)\s*=\s*(.*)$", line)
            if m:
                pairs.append((m.group(1), m.group(2).strip().strip('"').strip("'")))
    return pairs, text


def die(msg):
    print("\n❌ " + msg + "\n")
    sys.exit(1)


# Faqat bitta nusxa ishlasin: oldingi nusxani (agar ochiq qolgan bo'lsa) to'xtatamiz
PID_FILE = os.path.join(HERE, "bot.pid")
try:
    old_pid = int(open(PID_FILE).read().strip())
    if old_pid != os.getpid():
        import signal
        import time
        os.kill(old_pid, signal.SIGTERM)
        print(f"♻️  Oldingi bot nusxasi to'xtatildi (PID {old_pid})")
        time.sleep(2)
except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
    pass
open(PID_FILE, "w").write(str(os.getpid()))

pairs, env_text = read_env()
env = dict(pairs)

# 1) Telegram token
BOT_TOKEN = next((v for k, v in reversed(pairs) if k == "BOT_TOKEN" and TOKEN_RE.fullmatch(v)), None)
if not BOT_TOKEN:
    die(f"BOT_TOKEN topilmadi. {ENV_FILE} faylida BOT_TOKEN=... qatori bo'lishi kerak.")
try:
    r = httpx.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=20)
    if r.status_code != 200:
        die("Telegram token ishlamayapti. BotFather'dan yangi token oling.")
    BOT_NAME = r.json()["result"]["username"]
except httpx.HTTPError as e:
    die(f"Internetga ulanib bo'lmadi: {e}")
print(f"✅ Telegram bot: @{BOT_NAME}")

# 2) Vapi PRIVATE key: .env dagi barcha UUID'larni sinab, ishlaydiganini topadi
candidates = []
for k, v in pairs:
    if k == "VAPI_API_KEY":
        candidates.insert(0, v)
candidates += UUID_RE.findall(env_text)
VAPI_KEY, assistants = None, []
for c in dict.fromkeys(candidates):
    try:
        r = httpx.get(f"{VAPI}/assistant", headers={"Authorization": f"Bearer {c}"}, timeout=20)
    except httpx.HTTPError as e:
        die(f"Vapi'ga ulanib bo'lmadi: {e}")
    if r.status_code == 200:
        VAPI_KEY, assistants = c, r.json()
        break
if not VAPI_KEY:
    die("Vapi PRIVATE key topilmadi yoki ishlamayapti.\n"
        "   dashboard.vapi.ai → API Keys → Private Key ni nusxalang, keyin terminalda:\n"
        "   nano ~/vapi-bot/.env   →  VAPI_API_KEY=... qatoriga qo'ying (Ctrl+O, Enter, Ctrl+X)")
print(f"✅ Vapi kaliti ishlayapti ({VAPI_KEY[:8]}...)")

# 3) Assistant: .env dagisi bo'lsa o'sha, bo'lmasa birinchisi
if not assistants:
    die("Vapi'da birorta assistant yo'q. dashboard.vapi.ai → Assistants → Create Assistant.")
ids = {a["id"]: a for a in assistants}
wanted = env.get("VAPI_ASSISTANT_ID", "")
ASSISTANT = ids.get(wanted) or next((ids[u] for u in UUID_RE.findall(env_text) if u in ids), assistants[0])
ASSISTANT_ID = ASSISTANT["id"]
print(f"✅ Assistant: {ASSISTANT.get('name') or ASSISTANT_ID}")
if len(assistants) > 1:
    print("   (Boshqa assistantlar: " + ", ".join(a.get("name") or a["id"][:8] for a in assistants if a["id"] != ASSISTANT_ID) + ")")

# 4) Vapi PUBLIC key: suhbat ochish (/call/web) faqat public key bilan ishlaydi.
#    Private key orqali ro'yxatdan o'zi oladi, bo'lmasa yangisini yaratadi.
PRIV_H = {"Authorization": f"Bearer {VAPI_KEY}"}
ORIGIN = "https://zokirjon0063003.github.io"


def as_list(data):
    if isinstance(data, dict):
        data = data.get("results") or data.get("data") or []
    return data if isinstance(data, list) else []


PUBLIC_KEY = None
try:
    r = httpx.get(f"{VAPI}/token", headers=PRIV_H, timeout=20)
    tokens = as_list(r.json()) if r.status_code == 200 else []
    for t in tokens:
        rs = t.get("restrictions") or {}
        if t.get("tag") == "public" and t.get("value") and rs.get("enabled", True) is not False:
            allowed = rs.get("allowedAssistantIds")
            if not allowed or ASSISTANT_ID in allowed:
                PUBLIC_KEY = t["value"]
                break
    if not PUBLIC_KEY:
        r = httpx.post(f"{VAPI}/token", headers=PRIV_H, timeout=20,
                       json={"tag": "public", "name": "telegram-bot"})
        if r.status_code in (200, 201) and r.json().get("value"):
            PUBLIC_KEY = r.json()["value"]
            print("✅ Vapi'da yangi Public Key yaratildi (telegram-bot)")
except httpx.HTTPError as e:
    die(f"Vapi'ga ulanib bo'lmadi: {e}")
if not PUBLIC_KEY and UUID_RE.fullmatch(env.get("VAPI_PUBLIC_KEY", "")):
    PUBLIC_KEY = env["VAPI_PUBLIC_KEY"]
if not PUBLIC_KEY:
    die("Vapi Public Key'ni olib bo'lmadi. dashboard.vapi.ai → API Keys → Public Key ni\n"
        "   ~/vapi-bot/.env fayliga VAPI_PUBLIC_KEY=... qatori qilib yozing.")
print(f"✅ Vapi public key ({PUBLIC_KEY[:8]}...)")

# 5) Adminlar (default: 449766496)
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", env.get("ADMIN_IDS", ""))} or {449766496}
print(f"✅ Admin(lar): {', '.join(map(str, ADMIN_IDS))}")

# 6) Ovozlar (Mini App'da ayol/erkak tanlash uchun)
VOICE_PROVIDER = env.get("VOICE_PROVIDER") or "azure"
VOICE_FEMALE = env.get("VOICE_FEMALE") or "uz-UZ-MadinaNeural"
VOICE_MALE = env.get("VOICE_MALE") or "uz-UZ-SardorNeural"

# 7) Telefon raqam (AI telefon orqali qo'ng'iroq qilishi uchun) — Vapi'dan o'zi topadi
PHONE_ID, PHONE_LABEL = None, ""
try:
    r = httpx.get(f"{VAPI}/phone-number", headers={"Authorization": f"Bearer {VAPI_KEY}"}, timeout=20)
    numbers = as_list(r.json()) if r.status_code == 200 else []
except httpx.HTTPError:
    numbers = []
if numbers:
    pick = next((n for n in numbers if n.get("id") == env.get("VAPI_PHONE_NUMBER_ID")), numbers[0])
    PHONE_ID = pick["id"]
    PHONE_LABEL = f"{pick.get('number') or pick.get('name') or PHONE_ID[:8]} ({pick.get('provider', '?')})"
    print(f"✅ Telefon raqam: {PHONE_LABEL}")
    if pick.get("provider") == "vapi":
        print("   ⚠️ Vapi'ning bepul raqami faqat AQSh raqamlariga qo'ng'iroq qiladi, +998 ga emas.")
else:
    print("ℹ️  Vapi'da telefon raqam yo'q — telefon qo'ng'iroqlari o'chiq (Telnyx/Twilio raqamini ulang).")

# 8) Claude (Anthropic) — yozma xabarlarga javob berish uchun
ANTHROPIC_KEY = (env.get("ANTHROPIC_API_KEY") or "").strip()
CLAUDE_MODEL = (env.get("CLAUDE_MODEL") or "claude-opus-5-5").strip()
claude = None
if ANTHROPIC_KEY:
    try:
        import anthropic
        claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY)
        print(f"✅ Claude ({CLAUDE_MODEL}) yozma savollarga javob beradi")
    except ImportError:
        print("⚠️  'anthropic' kutubxonasi o'rnatilmagan:  pip install anthropic")
else:
    pass  # Claude ixtiyoriy

# 9) Google Gemini — bepul AI (karta talab qilmaydi)
GEMINI_KEY = (env.get("GEMINI_API_KEY") or "").strip()
GEMINI_MODEL = (env.get("GEMINI_MODEL") or "gemini-2.5-flash").strip()
print("✅ Gemini (bepul) yozma savollarga javob beradi" if GEMINI_KEY else
      "ℹ️  Gemini kaliti yo'q — Telegram'da adminga: /kalit AIza...")

# 10) OpenAI (ixtiyoriy) — yozma javoblar uchun
OPENAI_KEY = (env.get("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (env.get("OPENAI_MODEL") or "gpt-4.1-mini").strip()
if OPENAI_KEY:
    print(f"✅ OpenAI ({OPENAI_MODEL}) yozma savollarga javob beradi")

# .env ni toza holatda qayta yozamiz
with open(ENV_FILE, "w") as f:
    f.write(f"BOT_TOKEN={BOT_TOKEN}\nVAPI_API_KEY={VAPI_KEY}\nVAPI_PUBLIC_KEY={PUBLIC_KEY}\n"
            f"VAPI_ASSISTANT_ID={ASSISTANT_ID}\nADMIN_IDS={','.join(map(str, ADMIN_IDS))}\n"
            f"VOICE_PROVIDER={VOICE_PROVIDER}\nVOICE_FEMALE={VOICE_FEMALE}\nVOICE_MALE={VOICE_MALE}\n"
            + (f"VAPI_PHONE_NUMBER_ID={PHONE_ID}\n" if PHONE_ID else "")
            + (f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}\n" if ANTHROPIC_KEY else "")
            + (f"CLAUDE_MODEL={CLAUDE_MODEL}\n" if env.get("CLAUDE_MODEL") else "")
            + (f"GEMINI_API_KEY={GEMINI_KEY}\n" if GEMINI_KEY else "")
            + (f"GEMINI_MODEL={GEMINI_MODEL}\n" if env.get("GEMINI_MODEL") else "")
            + (f"OPENAI_API_KEY={OPENAI_KEY}\n" if OPENAI_KEY else "")
            + (f"OPENAI_MODEL={OPENAI_MODEL}\n" if env.get("OPENAI_MODEL") else ""))


def set_env(key, value):
    """.env dagi bitta qiymatni almashtiradi yoki qo'shadi."""
    txt = open(ENV_FILE).read()
    line = f"{key}={value}"
    txt = re.sub(rf"(?m)^{key}=.*$", line, txt) if re.search(rf"(?m)^{key}=", txt) else txt.rstrip("\n") + "\n" + line + "\n"
    open(ENV_FILE, "w").write(txt)
os.chmod(ENV_FILE, 0o600)

# ---------------------------------------------------------------- ma'lumotlar bazasi
import sqlite3
import asyncio
import html
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=5))  # Toshkent vaqti
db = sqlite3.connect(os.path.join(HERE, "users.db"))
db.execute("""CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT, name TEXT,
    first_seen TEXT, last_seen TEXT, starts INTEGER DEFAULT 0)""")
db.execute("CREATE TABLE IF NOT EXISTS seen_calls(id TEXT PRIMARY KEY)")
db.execute("CREATE TABLE IF NOT EXISTS chats(user_id INTEGER PRIMARY KEY, chat_id TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS faq(id INTEGER PRIMARY KEY AUTOINCREMENT, keywords TEXT, answer TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS op_msgs(admin_chat INTEGER, msg_id INTEGER, user_id INTEGER, PRIMARY KEY(admin_chat, msg_id))")
db.execute("CREATE TABLE IF NOT EXISTS contacts(name TEXT PRIMARY KEY COLLATE NOCASE, phone TEXT)")
db.execute("""CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, text TEXT, ts TEXT)""")
db.commit()


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def track(user):
    """Foydalanuvchini saqlaydi. Yangi bo'lsa True qaytaradi."""
    name = " ".join(filter(None, [user.first_name, user.last_name]))
    row = db.execute("SELECT id FROM users WHERE id=?", (user.id,)).fetchone()
    if row:
        db.execute("UPDATE users SET username=?, name=?, last_seen=?, starts=starts+1 WHERE id=?",
                   (user.username or "", name, now_iso(), user.id))
    else:
        db.execute("INSERT INTO users VALUES(?,?,?,?,?,1)",
                   (user.id, user.username or "", name, now_iso(), now_iso()))
    db.commit()
    return row is None


def fmt_time(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ).strftime("%d.%m %H:%M")
    except ValueError:
        return iso[:16]


def who(uid, username, name):
    e = html.escape
    tag = f" @{e(username)}" if username else ""
    return f'<a href="tg://user?id={uid}">{e(name or "—")}</a>{tag} <code>{uid}</code>'


def is_admin(update: Update):
    return update.effective_user and update.effective_user.id in ADMIN_IDS


# ---------------------------------------------------------------- Vapi suhbatlari
async def fetch_calls(limit=20):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{VAPI}/call", headers=PRIV_H,
                             params={"assistantId": ASSISTANT_ID, "limit": limit})
    r.raise_for_status()
    calls = as_list(r.json())
    calls.sort(key=lambda c: c.get("createdAt", ""), reverse=True)
    return calls


def call_user(c):
    md = ((c.get("assistantOverrides") or {}).get("metadata")) or c.get("metadata") or {}
    uid = md.get("tg_id")
    phone = (c.get("customer") or {}).get("number")
    if phone:
        return f"📱 {html.escape(md.get('contact') or '')} <code>{html.escape(phone)}</code>".replace("📱  ", "📱 ")
    if not uid:
        return "noma'lum (brauzer)"
    row = db.execute("SELECT username, name FROM users WHERE id=?", (int(uid),)).fetchone()
    username, name = (row or (md.get("tg_username"), md.get("tg_name")))
    return who(uid, username, name)


def call_seconds(c):
    try:
        a = datetime.fromisoformat(c["startedAt"].replace("Z", "+00:00"))
        b = datetime.fromisoformat(c["endedAt"].replace("Z", "+00:00"))
        return max(0, int((b - a).total_seconds()))
    except (KeyError, TypeError, ValueError):
        return 0


def call_line(i, c):
    sec = call_seconds(c)
    summary = ((c.get("analysis") or {}).get("summary") or "").strip()
    status = "🟢 davom etmoqda" if c.get("status") != "ended" else f"⏱ {sec // 60}:{sec % 60:02d}"
    cost = f" · ${c['cost']:.2f}" if isinstance(c.get("cost"), (int, float)) else ""
    s = f"<b>{i}.</b> {fmt_time(c.get('createdAt'))} · {status}{cost}\n👤 {call_user(c)}"
    if summary:
        s += f"\n📝 {html.escape(summary[:300])}"
    return s


# ---------------------------------------------------------------- foydalanuvchi tomoni
def build_start_kb():
    link = WEBAPP_URL + "?" + urlencode({"k": PUBLIC_KEY, "a": ASSISTANT_ID,
                                         "vp": VOICE_PROVIDER, "vf": VOICE_FEMALE, "vm": VOICE_MALE})
    return link, InlineKeyboardMarkup([
        [InlineKeyboardButton("🎙 AI bilan gaplashish", web_app=WebAppInfo(url=link))],
        [InlineKeyboardButton("🌐 Brauzerda ochish", url=link)],
        [InlineKeyboardButton("📚 Katalog", url="https://claude.ai/artifact/65qGVW5DsKiduNyTDHxFbj"),
         InlineKeyboardButton("🔥 Aksiya", url="https://t.me/SUPER7677")],
    ])


APP_LINK, START_KB = build_start_kb()


async def notify_admins(context, text, kb=None):
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, text, parse_mode="HTML", reply_markup=kb,
                                           disable_web_page_preview=True)
        except Exception as e:  # admin botni bloklagan bo'lishi mumkin
            log.warning("Adminga yuborilmadi %s: %s", aid, e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if track(u) and u.id not in ADMIN_IDS:
        total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        await notify_admins(context, f"🆕 Yangi foydalanuvchi #{total}\n{who(u.id, u.username, u.full_name)}")
    text = ("Assalomu alaykum! Men Sabah AI 🤖\n\n"
            "✍️ Savolingizni shu yerga yozing — javob beraman.\n"
            "🎙 Yoki tugmani bosib, men bilan ovozli gaplashing (ayol yoki erkak ovozini tanlang).")
    if u.id in ADMIN_IDS:
        text += "\n\n🛠 Admin panel: /admin"
    await update.message.reply_text(text, reply_markup=START_KB)


async def talk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # eski xabarlardagi tugma uchun
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Yangi tugma:", reply_markup=START_KB)


# ---------------------------------------------------------------- yozma suhbat (Vapi Chat)
def save_msg(uid, role, text):
    db.execute("INSERT INTO messages(user_id, role, text, ts) VALUES(?,?,?,?)", (uid, role, text, now_iso()))
    db.commit()


def chat_output(data):
    out = data.get("output") or []
    texts = [m.get("content") for m in out if isinstance(m, dict) and m.get("role") == "assistant" and m.get("content")]
    return "\n\n".join(t if isinstance(t, str) else str(t) for t in texts).strip()


# ---------------------------------------------------------------- telefon qo'ng'iroqlari (faqat admin)
CALL_WORDS = re.compile(r"\b(ter|terib|tering|qo['`ʻ’]?ng['`ʻ’]?iroq|qongiroq|zvon\w*|позвони\w*|call)\b", re.I)


def norm_phone(raw):
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 9:
        d = "998" + d
    if len(d) == 10 and d.startswith("0"):
        d = "998" + d[1:]
    return "+" + d if 9 <= len(d) <= 15 else None


def find_phone(text):
    m = re.search(r"\+?\d[\d\s\-()]{7,}\d", text)
    return (norm_phone(m.group(0)), m.group(0)) if m else (None, None)


def find_contact(text):
    for name, phone in db.execute("SELECT name, phone FROM contacts ORDER BY length(name) DESC"):
        if re.search(r"\b" + re.escape(name.lower()) + r"(ga|ka|qa|ni|ning)?\b", text.lower()):
            return name, phone
    return None, None


def call_purpose(text, *remove):
    t = text
    for x in remove:
        if x:
            t = re.sub(re.escape(x) + r"(ga|ka|qa|ni|ning)?", " ", t, flags=re.I)
    t = CALL_WORDS.sub(" ", t)
    t = re.sub(r"\b(ga|ka|qa|qil|qiling|qilib|va|ham)\b", " ", t, flags=re.I)
    return re.sub(r"[\s,.:;-]+", " ", t).strip(" ,.")


async def ask_call(update, context, phone, name, purpose):
    if not PHONE_ID:
        await update.message.reply_text(
            "📵 Telefon qo'ng'iroqlari hali yoqilmagan: Vapi'da telefon raqam yo'q.\n"
            "Telnyx yoki Twilio raqamini dashboard.vapi.ai → Phone Numbers → Import orqali ulang, "
            "keyin botni qayta ishga tushiring.")
        return
    key = f"c{update.message.message_id}"
    context.user_data[key] = {"phone": phone, "name": name or "", "purpose": purpose}
    who_txt = f"{name} ({phone})" if name else phone
    extra = f"\n🎯 Maqsad: {purpose}" if purpose else ""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Ha, qo'ng'iroq qil", callback_data=f"dial:{key}"),
                                InlineKeyboardButton("❌ Bekor", callback_data=f"nodial:{key}")]])
    await update.message.reply_text(f"📞 {who_txt} ga qo'ng'iroq qilaymi?{extra}", reply_markup=kb)


async def dial_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Ruxsat yo'q", show_alert=True)
        return
    action, key = q.data.split(":", 1)
    job = context.user_data.pop(key, None)
    await q.answer()
    if action == "nodial" or not job:
        await q.edit_message_text("❌ Bekor qilindi." if action == "nodial" else "Bu so'rov eskirgan, qaytadan yozing.")
        return
    await q.edit_message_text(f"⏳ {job['name'] or job['phone']} ga qo'ng'iroq qilinmoqda…")
    body = {
        "assistantId": ASSISTANT_ID,
        "phoneNumberId": PHONE_ID,
        "customer": {"number": job["phone"], **({"name": job["name"]} if job["name"] else {})},
        "assistantOverrides": {
            "variableValues": {"name": job["name"], "maqsad": job["purpose"]},
            "metadata": {"contact": job["name"], "purpose": job["purpose"], "by_admin": str(q.from_user.id)},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{VAPI}/call", headers=PRIV_H, json=body)
    except httpx.HTTPError as e:
        await q.edit_message_text(f"Vapi bilan aloqa xatosi: {e}")
        return
    if r.status_code in (200, 201):
        c = r.json()
        await q.edit_message_text(
            f"📞 Qo'ng'iroq boshlandi: {job['name'] or ''} {job['phone']}\nID: {c.get('id')}\n"
            "Tugagach natijasi (xulosa va matn) shu yerga keladi.")
    else:
        log.error("Qo'ng'iroq xatosi %s: %s", r.status_code, r.text[:500])
        await q.edit_message_text(f"❌ Vapi qo'ng'iroqni qabul qilmadi ({r.status_code}):\n{r.text[:400]}")


async def call_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/call +998901234567 [maqsad]  yoki  /call Ali [maqsad]"""
    if not is_admin(update):
        return
    arg = update.message.text.partition(" ")[2].strip()
    phone, raw = find_phone(arg)
    name, cphone = (None, None) if phone else find_contact(arg)
    if not (phone or cphone):
        await update.message.reply_text("Foydalanish:\n/call +998901234567 buyurtmani tasdiqlash\n/call Ali\n\n"
                                        "Yoki oddiy yozing: «Aliga ter» yoki «+998901234567 ga qo'ng'iroq qil»")
        return
    await ask_call(update, context, phone or cphone, name, call_purpose(arg, raw, name))


async def contact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kontakt Ali +998901234567  — saqlash;  /kontakt — ro'yxat;  /kontakt Ali o'chir"""
    if not is_admin(update):
        return
    arg = update.message.text.partition(" ")[2].strip()
    if not arg:
        rows = db.execute("SELECT name, phone FROM contacts ORDER BY name").fetchall()
        txt = "\n".join(f"• {n} — {p}" for n, p in rows) or "Hali kontakt yo'q."
        await update.message.reply_text(f"📇 Kontaktlar:\n{txt}\n\nQo'shish: /kontakt Ali +998901234567\nO'chirish: /kontakt Ali o'chir")
        return
    phone, raw = find_phone(arg)
    if not phone:
        nm = re.sub(r"\s+(o['`ʻ’]?chir|delete)$", "", arg, flags=re.I).strip()
        if nm != arg:
            db.execute("DELETE FROM contacts WHERE name=?", (nm,))
            db.commit()
            await update.message.reply_text(f"🗑 {nm} o'chirildi.")
        else:
            await update.message.reply_text("Foydalanish: /kontakt Ali +998901234567")
        return
    name = arg.replace(raw, " ").strip(" ,-:")
    if not name:
        await update.message.reply_text("Ismni ham yozing: /kontakt Ali +998901234567")
        return
    db.execute("INSERT OR REPLACE INTO contacts VALUES(?,?)", (name, phone))
    db.commit()
    await update.message.reply_text(f"✅ Saqlandi: {name} — {phone}\nEndi «{name}ga ter» deb yozsangiz bo'ladi.")


# ---------------------------------------------------------------- Claude bilan yozma suhbat
CHAT_RULES = (
    "\n\n---\nHozir sen Telegram'da YOZMA chatdasan (ovozli emas). Mijozga o'zbek tilida, qisqa va aniq "
    "(odatda 1–4 gap) javob ber. Markdown belgilari (**, #, jadval) ishlatma — oddiy matn va kerak bo'lsa emoji. "
    "Ovozli suhbat kerak bo'lsa, mijozga «🎙 AI bilan gaplashish» tugmasini eslat."
)


def vapi_system_prompt():
    """Vapi assistantdagi System Prompt — AI ovozda ham, yozuvda ham bir xil gapirishi uchun."""
    model = (ASSISTANT or {}).get("model") or {}
    parts = [m.get("content", "") for m in model.get("messages") or [] if m.get("role") == "system"]
    base = "\n\n".join(p for p in parts if isinstance(p, str) and p.strip())
    return base or "Sen Sabah AI — Sabah ko'rpa-to'shak brendining savdo yordamchisisan."


SABAH_PERSONA = (
    "Sen «Sabah AI» — Sabaev Home (Sabah) do'konining rasmiy savdo yordamchisisan. Do'kon uy tekstili uchun "
    "MATOLAR sotadi: ranfors, satin, super satin, pike, muleton va boshqalar — rulon yoki metr hisobida, ulgurji ham.\n"
    "QOIDALAR:\n"
    "1. Faqat Sabaev Home mahsulotlarini taklif qil. Mijozni xaridga undab tur.\n"
    "2. HECH QACHON boshqa do'kon, bozor, marketplace (OLX, Wildberries, Uzum, Ozon va h.k.) yoki raqobatchini tavsiya qilma.\n"
    "3. Mahsulot haqida so'rashsa — quyidagi ro'yxatdan mos matoni va uning Telegram havolasini ber "
    "(havolani to'liq yoz, masalan https://t.me/Pastel9006/20846).\n"
    "4. «Qayerdan olsam bo'ladi?», «buyurtma», «manzil» savollarida: katalog sahifasi, kanal, admin va telefon raqamini ber.\n"
    "5. Narxni bilmaysan — o'ylab topma: «narx va mavjudlik uchun adminga yozing» deb, mos admin kontaktini ber.\n"
    "6. Buyurtma uchun mijozdan mato turi, rang/kod (masalan SS-103) va miqdorini (rulon yoki metr) so'ra.\n"
    "7. Mijozlar sheva va ruscha so'zlar ishlatadi: «silka», «ssilka», «ссылка», «link» = HAVOLA. "
    "Havola so'rashsa — katalog havolasini va mos kanal/post havolasini ber. «adres» = manzil, «nomer» = telefon, "
    "«skidka»/«aksiya» = chegirma (aksiya kanalini ber), «optom» = ulgurji, «metrlik» = metr hisobida.\n"
    "8. Agar so'z tushunarsiz bo'lsa, o'ylab topma — mijozdan aniqlashtirib so'ra."
)

DEFAULT_INFO = """KATALOG (hamma mahsulot bir sahifada): https://claude.ai/artifact/65qGVW5DsKiduNyTDHxFbj
Ish vaqti: 06:00–18:00. Yetkazib berish bor. Do'kondan olib ketish ham mumkin.

TURKIYA MAHSULOTLARI — manzil: Abu Saxiy, Uyg'ur bozori, E-8. Kanal: https://t.me/Sabaevhomeuz
Admin: @sabaev_admin, +998 90 099 09 44 · Nurullo: @Nurulloh_571, +998 99 740 00 62
- Ranfors (100% paxta, eni 2.40 m, choyshab uchun): https://t.me/Sabaevhomeuz/10550
  Ranfors print https://t.me/Sabaevhomeuz/10289 · Ranfors adnatoni https://t.me/Sabaevhomeuz/10052
  Ranfors 5D https://t.me/Sabaevhomeuz/9925 · Ranfors 4D https://t.me/Sabaevhomeuz/9974
  Ranfors jakkard https://t.me/Sabaevhomeuz/10029 · Ranfors gofra https://t.me/Sabaevhomeuz/9651
- Satin: Strayp https://t.me/Sabaevhomeuz/10085 · Jakkard https://t.me/Sabaevhomeuz/10020 ·
  Digital https://t.me/Sabaevhomeuz/10007 · Adnatoni https://t.me/Sabaevhomeuz/9137
- Pike va boshqalar: Wafli pike https://t.me/Sabaevhomeuz/10185 · Mayda pike https://t.me/Sabaevhomeuz/10193 ·
  Zanjir pike https://t.me/Sabaevhomeuz/10203 · Digital vafli pike https://t.me/Sabaevhomeuz/9847 ·
  Turkiya vafel https://t.me/Sabaevhomeuz/9817 · Duck https://t.me/Sabaevhomeuz/9604 ·
  Digital duck https://t.me/Sabaevhomeuz/9531 · Laviva print https://t.me/Sabaevhomeuz/10114
- Muleton (suv o'tkazmas): https://t.me/Sabaevhomeuz/10750

XITOY MAHSULOTLARI — manzil: Abu Saxiy, Uyg'ur bozori, E-76-77. Kanal: https://t.me/Pastel9006
Komilbek: @komil_1247, +998 90 021 12 47 · Ziyodillo: @Ziyodillo70, +998 99 302 97 70
Ulgurji: 1 rulondan, alohida narx.
- Super satin (ikki tomoni bor, eni 2.40 m, 100% paxta, kodlar SS-…):
  Bolalar (SS-274–314) https://t.me/Pastel9006/20846 · Bir rangli (SS-267–273) https://t.me/Pastel9006/20832 ·
  Kattalar (SS-210–266) https://t.me/Pastel9006/20775 · SS kodlar https://t.me/Pastel9006/20701 ·
  Boshqa ranglar https://t.me/Pastel9006/20482
- Yevro satin (oq yevro satin eni 2.60 m): Oq https://t.me/Pastel9006/19981 · Bir rangli https://t.me/Pastel9006/19025 ·
  Jakkard satin https://t.me/Pastel9006/19369 · Oq jakkard https://t.me/Pastel9006/19418 · Baykovi satin https://t.me/Pastel9006/19979
- 5D digital https://t.me/Pastel9006/20334 · Strayp satin mehmonxona https://t.me/Pastel9006/20401 ·
  Tencel https://t.me/Pastel9006/20470 · Muleton https://t.me/Pastel9006/20729

AKSIYA va chegirmalar kanali: https://t.me/SUPER7677
Instagram: https://www.instagram.com/sabaevhome.uz

Qanday buyurtma beriladi: 1) mato turini tanlab havola orqali ko'ring; 2) rang, kod va miqdorni (rulon yoki metr) yozing;
3) narx va yetkazib berish bo'yicha kelishamiz; 4) bozordagi do'kondan olib keting yoki yetkazib berishni tanlang."""


def get_setting(key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def chat_system(u):
    parts = [SABAH_PERSONA]
    info = get_setting("info") or DEFAULT_INFO
    if info:
        parts.append("SABAH HAQIDA MA'LUMOT (kanallar, aloqa, mahsulotlar — shularga tayan):\n" + info)
    faq = "\n".join(f"- {a}" for (a,) in db.execute("SELECT answer FROM faq"))
    if faq:
        parts.append("Tayyor ma'lumotlar:\n" + faq)
    vp = vapi_system_prompt()
    if "sabah" in vp.lower():  # Vapi'dagi prompt Sabah haqida bo'lsa, uni ham qo'shamiz
        parts.append(vp)
    if u and u.first_name:
        parts.append(f"Mijozning ismi: {u.first_name}.")
    text = "\n\n".join(parts)
    text = re.sub(r"\{\{\s*name\s*\}\}", (u.first_name if u else "") or "", text)
    text = re.sub(r"\{\{\s*maqsad\s*\}\}", "", text)
    return text + CHAT_RULES


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/info matn — AI biladigan Sabah ma'lumotlari (kanallar, aloqa, mahsulotlar, narxlar)."""
    if not is_admin(update):
        return
    arg = update.message.text.partition(" ")[2].strip()
    if not arg:
        cur = get_setting("info") or ("(standart: Sabaev Home katalogi)\n\n" + DEFAULT_INFO[:1500] + "…")
        await update.message.reply_text(
            f"📝 AI biladigan Sabah ma'lumotlari:\n\n{cur}\n\n"
            "O'zgartirish uchun /info dan keyin hammasini qaytadan yozing, masalan:\n\n"
            "/info Telegram kanal: https://t.me/sabah_kanal\nInstagram: https://instagram.com/sabah\n"
            "Telefon: +998 90 123 45 67\nManzil: Toshkent, ...\nSuper satin 2 kishilik — 450 000 so'm\nYetkazib berish: ...")
        return
    db.execute("INSERT OR REPLACE INTO settings VALUES('info', ?)", (arg,))
    db.commit()
    await update.message.reply_text("✅ Saqlandi. Endi AI shu ma'lumotlarga tayanib javob beradi.")


def claude_history(uid, limit=20):
    rows = db.execute("SELECT role, text FROM messages WHERE user_id=? AND id > COALESCE("
                      "(SELECT MAX(id) FROM messages WHERE user_id=? AND role='reset'), 0) "
                      "ORDER BY id DESC LIMIT ?", (uid, uid, limit)).fetchall()
    msgs = []
    for role, text in reversed(rows):
        r = "assistant" if role == "ai" else "user"
        if msgs and msgs[-1]["role"] == r:
            msgs[-1]["content"] += "\n" + text  # ketma-ket bir xil rol — birlashtiramiz
        else:
            msgs.append({"role": r, "content": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


async def claude_reply(u):
    system = chat_system(u)
    resp = await claude.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=system,
        messages=claude_history(u.id),
        output_config={"effort": "low"},          # chat: tez va arzon
        cache_control={"type": "ephemeral"},      # System Prompt keshlanadi
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",                      # rad etilsa, boshqa model davom ettiradi
    )
    if resp.stop_reason == "refusal":
        return "Kechirasiz, bu savolga javob bera olmayman. Sabah mahsulotlari haqida so'rang 🙂"
    return "".join(b.text for b in resp.content if b.type == "text").strip()


# ---------------------------------------------------------------- FAQ, Gemini, operator
def norm(t):
    return re.sub(r"['`ʻ’‘]", "", (t or "").lower())


GREET_RE = re.compile(r"^\W*(salom|assalom\w*|asalom\w*|salomaleykum|hayrli|привет|здравствуйте|hello|hi)(\s+\w+)?\W*$", re.I)
LINK_RE = re.compile(r"\b(s+ilka\w*|s+ilk[ai]|ссылк\w*|link\w*|havola\w*|katalog\w*|каталог\w*|kanal\w*)\b", re.I)
THANKS_RE = re.compile(r"^\W*(rahmat|raxmat|tashakkur|спасибо|thanks?|thank you)\W*\w*\W*$", re.I)


def builtin_answer(text, u):
    """AI'siz ham ishlaydigan oddiy javoblar."""
    if GREET_RE.match(text):
        return (f"Va alaykum assalom, {u.first_name or 'hurmatli mijoz'}! 😊 Men Sabah AI.\n"
                "Savolingizni yozing yoki 🎙 tugmasini bosib men bilan ovozli gaplashing.")
    if LINK_RE.search(text):
        return ("📚 Katalog (hamma matolar): https://claude.ai/artifact/65qGVW5DsKiduNyTDHxFbj\n"
                "🇹🇷 Turkiya matolari: https://t.me/Sabaevhomeuz\n"
                "🇨🇳 Xitoy matolari (super satin): https://t.me/Pastel9006\n"
                "🔥 Aksiya: https://t.me/SUPER7677\n"
                "📞 Admin: @sabaev_admin, +998 90 099 09 44")
    if THANKS_RE.match(text):
        return "Arzimaydi! 😊 Yana savol bo'lsa, bemalol yozing."
    return None


def faq_match(text):
    t, best, score = norm(text), None, 0
    for kw, ans in db.execute("SELECT keywords, answer FROM faq"):
        hits = sum(1 for k in kw.split(",") if norm(k).strip() and norm(k).strip() in t)
        if hits > score:
            best, score = ans, hits
    return best


async def gemini_reply(u):
    system = chat_system(u)
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in claude_history(u.id)] or [{"role": "user", "parts": [{"text": "Salom"}]}]
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            json={"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                  "generationConfig": {"maxOutputTokens": 800}},
        )
    if r.status_code != 200:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
    cands = r.json().get("candidates") or []
    parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
    return "".join(p.get("text", "") for p in parts).strip()


async def openai_reply(u):
    system = chat_system(u)
    msgs = [{"role": "system", "content": system}] + (claude_history(u.id) or [{"role": "user", "content": "Salom"}])
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions",
                              headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                              json={"model": OPENAI_MODEL, "messages": msgs, "max_tokens": 800})
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:300]}")
    return ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()


async def to_operator(update, context, text):
    u = update.effective_user
    await update.message.reply_text("Savolingiz operatorimizga yuborildi, tez orada javob beramiz 🙏")
    for aid in ADMIN_IDS:
        try:
            m = await context.bot.send_message(
                aid, f"❓ <b>Savol</b> — {who(u.id, u.username, u.full_name)}\n\n{html.escape(text)}\n\n"
                     "<i>Javob berish uchun shu xabarga «Reply» qilib yozing.</i>", parse_mode="HTML")
            db.execute("INSERT OR REPLACE INTO op_msgs VALUES(?,?,?)", (aid, m.message_id, u.id))
            db.commit()
        except Exception as e:
            log.warning("Operatorga yuborilmadi: %s", e)


async def admin_reply(update, context):
    """Admin savol xabariga Reply qilsa — javob mijozga boradi. True qaytarsa, xabar shu yerda tugadi."""
    rt = update.message.reply_to_message
    if not rt:
        return False
    row = db.execute("SELECT user_id FROM op_msgs WHERE admin_chat=? AND msg_id=?",
                     (update.effective_chat.id, rt.message_id)).fetchone()
    if not row:
        return False
    try:
        await context.bot.send_message(row[0], f"👩‍💼 Sabah operatori:\n{update.message.text}")
        save_msg(row[0], "ai", update.message.text)
        await update.message.reply_text("✅ Javob mijozga yuborildi.")
    except Exception as e:
        await update.message.reply_text(f"Yuborilmadi: {e}")
    return True


async def faq_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    arg = update.message.text.partition(" ")[2].strip()
    m = re.match(r"(?i)^o['`ʻ’]?chir\s+(\d+)$", arg)
    if m:
        db.execute("DELETE FROM faq WHERE id=?", (int(m.group(1)),))
        db.commit()
        await update.message.reply_text(f"🗑 {m.group(1)}-javob o'chirildi.")
        return
    if "=" in arg:
        kw, _, ans = arg.partition("=")
        kw = ",".join(k.strip().lower() for k in kw.split(",") if k.strip())
        if not kw or not ans.strip():
            await update.message.reply_text("Format: /faq narx, qancha = Javob matni")
            return
        db.execute("INSERT INTO faq(keywords, answer) VALUES(?,?)", (kw, ans.strip()))
        db.commit()
        await update.message.reply_text(f"✅ Saqlandi. Kalit so'zlar: {kw}")
        return
    rows = db.execute("SELECT id, keywords, answer FROM faq ORDER BY id").fetchall()
    lst = "\n\n".join(f"{i}. 🔑 {k}\n💬 {a[:150]}" for i, k, a in rows) or "Hali tayyor javob yo'q."
    await update.message.reply_text(
        f"📚 Tayyor javoblar:\n\n{lst}\n\n"
        "Qo'shish:\n/faq narx, qancha, нарх = 2 kishilik to'plam 450 000 so'm\n"
        "O'chirish:\n/faq o'chir 2")


async def key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kalit AIza...  (Gemini)  yoki  /kalit sk-ant-...  (Claude) — kalit .env ga yoziladi."""
    global GEMINI_KEY, OPENAI_KEY, claude, anthropic
    if not is_admin(update):
        return
    k = update.message.text.partition(" ")[2].strip()
    try:
        await update.message.delete()  # kalit chatda qolmasin
    except Exception:
        pass
    chat = update.effective_chat.id
    if k.startswith("AIza"):
        GEMINI_KEY = k
        set_env("GEMINI_API_KEY", k)
        try:
            test = await gemini_reply(update.effective_user)
            await context.bot.send_message(chat, f"✅ Gemini ulandi va ishlayapti.\nSinov javobi: {test[:200]}")
        except Exception as e:
            await context.bot.send_message(chat, f"⚠️ Kalit saqlandi, lekin Gemini xato berdi:\n{str(e)[:300]}")
    elif k.startswith("sk-ant-"):
        try:
            import anthropic as _a
            anthropic = _a
            claude = _a.AsyncAnthropic(api_key=k)
            set_env("ANTHROPIC_API_KEY", k)
            await context.bot.send_message(chat, "✅ Claude kaliti saqlandi.")
        except ImportError:
            await context.bot.send_message(chat, "Avval terminalda: pip install anthropic")
    elif k.startswith("sk-"):
        OPENAI_KEY = k
        set_env("OPENAI_API_KEY", k)
        try:
            test = await openai_reply(update.effective_user)
            await context.bot.send_message(chat, f"✅ OpenAI ulandi va ishlayapti.\nSinov javobi: {test[:200]}")
        except Exception as e:
            await context.bot.send_message(chat, f"⚠️ Kalit saqlandi, lekin OpenAI xato berdi:\n{str(e)[:300]}")
    else:
        await context.bot.send_message(chat, "Kalit tanilmadi. Gemini «AIza…», OpenAI «sk-…», Claude «sk-ant-…» bilan boshlanadi.\n"
                                             "Gemini kalitini bepul olish: aistudio.google.com → Get API key")


# ---------------------------------------------------------------- RASM orqali qidirish
# Kanallardagi rasmlar t.me/s/<kanal> sahifasidan yig'iladi va "dHash" barmoq izi bilan saqlanadi.
# Mijoz rasm yuborsa, shu barmoq izlari bilan solishtiriladi.
import base64
import io
import time as _time

CHANNELS = [c.strip() for c in (env.get("CHANNELS") or "Sabaevhomeuz,Pastel9006,SUPER7677").split(",") if c.strip()]
MAX_PAGES = int(env.get("INDEX_PAGES") or 60)        # har kanal uchun ~20 post/sahifa
MATCH_DIST = int(env.get("MATCH_DISTANCE") or 9)      # 0..64; kichik = qattiqroq
db.execute("""CREATE TABLE IF NOT EXISTS images(
    url TEXT PRIMARY KEY, post TEXT, caption TEXT, h1 TEXT, h2 TEXT, ts REAL)""")
db.commit()
try:
    from PIL import Image, ImageOps, ImageChops
except ImportError:
    Image = None
    print("⚠️  Rasm qidirish uchun Pillow kerak:  pip install pillow")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15"}
INDEX_STATE = {"running": False, "done": 0, "last": 0.0}


def _dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (px[r * (size + 1) + c] > px[r * (size + 1) + c + 1])
    return bits


def _trim(img):
    """Skrinshot chetlaridagi bir xil rangli hoshiyani olib tashlaydi."""
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    box = ImageChops.difference(rgb, bg).convert("L").point(lambda v: 255 if v > 25 else 0).getbbox()
    return rgb.crop(box) if box and (box[2] - box[0]) > 50 and (box[3] - box[1]) > 50 else rgb


def _variants(img):
    img = ImageOps.exif_transpose(img).convert("RGB")
    t = _trim(img)
    return [img, t, _center(t, 0.85), _center(t, 0.7)]  # skrinshot va kadrlash uchun


def _center(img, f):
    w, h = img.size
    dw, dh = int(w * (1 - f) / 2), int(h * (1 - f) / 2)
    return img.crop((dw, dh, w - dw, h - dh))


def image_hashes(data):
    """Kanal rasmi uchun barmoq izlari: to'liq, markazdan kesilgan (85%, 70%) va oynadagi aksi."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    h1 = ",".join(f"{_dhash(v):016x}" for v in (img, _center(img, 0.85), _center(img, 0.7)))
    h2 = f"{_dhash(ImageOps.mirror(img)):016x}"
    return h1, h2


def _ham(a, b):
    return bin(a ^ b).count("1")


def find_match(data, rows):
    """Eng yaqin kanal rasmini topadi: (masofa, post havolasi, matn) yoki None."""
    if not rows:
        return None
    img = Image.open(io.BytesIO(data))
    qs = [_dhash(v) for v in _variants(img)]
    best = None
    for post, cap, h1, h2 in rows:
        hs = [int(x, 16) for x in (h1 + "," + h2).split(",") if x]
        d = min(_ham(q, h) for q in qs for h in hs)
        if best is None or d < best[0]:
            best = (d, post, cap or "")
    return best


POST_RE = re.compile(r'data-post="([^"]+)"(.*?)(?=data-post="|\Z)', re.S)
IMG_RE = re.compile(r"tgme_widget_message_photo_wrap[^>]*?background-image:url\('([^']+)'\)", re.S)
TXT_RE = re.compile(r'tgme_widget_message_text[^>]*>(.*?)</div>', re.S)


def parse_channel_page(html_text):
    """t.me/s/<kanal> sahifasidan [(post, [rasm url], matn)] ro'yxati."""
    out = []
    for post, body in POST_RE.findall(html_text):
        imgs = IMG_RE.findall(body)
        m = TXT_RE.search(body)
        txt = re.sub(r"<br\s*/?>", "\n", m.group(1)) if m else ""
        txt = html.unescape(re.sub(r"<[^>]+>", "", txt)).strip()
        if imgs:
            out.append((post, imgs, txt))
    return out


async def build_index(force=False):
    if Image is None or INDEX_STATE["running"]:
        return
    INDEX_STATE["running"] = True
    added = 0
    try:
        async with httpx.AsyncClient(timeout=30, headers=UA, follow_redirects=True) as client:
            for ch in CHANNELS:
                before = None
                for _ in range(MAX_PAGES):
                    url = f"https://t.me/s/{ch}" + (f"?before={before}" if before else "")
                    try:
                        r = await client.get(url)
                    except httpx.HTTPError as e:
                        log.warning("Kanal sahifasi o'qilmadi %s: %s", url, e)
                        break
                    posts = parse_channel_page(r.text)
                    if not posts:
                        break
                    new_here = 0
                    for post, imgs, txt in posts:
                        for iu in imgs:
                            if not force and db.execute("SELECT 1 FROM images WHERE url=?", (iu,)).fetchone():
                                continue
                            try:
                                ir = await client.get(iu)
                                h1, h2 = await asyncio.to_thread(image_hashes, ir.content)
                            except Exception:
                                continue
                            db.execute("INSERT OR REPLACE INTO images VALUES(?,?,?,?,?,?)",
                                       (iu, f"https://t.me/{post}", txt[:500], h1, h2, _time.time()))
                            added += 1
                            new_here += 1
                    db.commit()
                    INDEX_STATE["done"] = db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
                    ids = [int(p.split("/")[-1]) for p, _, _ in posts if p.split("/")[-1].isdigit()]
                    if not ids or (new_here == 0 and not force and before is not None):
                        break  # eski postlar allaqachon indeksda
                    before = min(ids)
                    await asyncio.sleep(0.5)
    finally:
        INDEX_STATE["running"] = False
        INDEX_STATE["last"] = _time.time()
        total = db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        log.info("Rasm indeksi yangilandi: +%d, jami %d ta rasm", added, total)


async def index_loop(app):
    while True:
        try:
            await build_index()
        except Exception as e:
            log.warning("Rasm indeksi xatosi: %s", e)
        await asyncio.sleep(6 * 3600)  # har 6 soatda yangi postlarni qo'shadi


async def vision_reply(u, data, question):
    """AI rasmga qarab katalogdan o'xshash bo'limni taklif qiladi."""
    b64 = base64.b64encode(data).decode()
    ask = ("Mijoz rasm yubordi" + (f" va yozdi: «{question}»" if question else "") +
           ". Rasmdagi mato/choyshabni qisqa tasvirla (rang, naqsh, bolalar/kattalar uchun) va katalogdan eng "
           "o'xshash bo'limni HAVOLASI bilan taklif qil. Aynan shu rasm bor deb aniq aytma — "
           "aniq javob uchun admin tekshirishini ayt.")
    system = chat_system(u)
    if OPENAI_KEY:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post("https://api.openai.com/v1/chat/completions",
                                  headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                                  json={"model": OPENAI_MODEL, "max_tokens": 600, "messages": [
                                      {"role": "system", "content": system},
                                      {"role": "user", "content": [
                                          {"type": "text", "text": ask},
                                          {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]})
        if r.status_code == 200:
            return ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
        log.error("OpenAI rasm xatosi %s: %s", r.status_code, r.text[:300])
    if GEMINI_KEY:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEY},
                json={"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": ask},
                                                              {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}]})
        if r.status_code == 200:
            c = (r.json().get("candidates") or [{}])[0]
            return "".join(p.get("text", "") for p in ((c.get("content") or {}).get("parts") or [])).strip()
        log.error("Gemini rasm xatosi %s: %s", r.status_code, r.text[:300])
    return ""


async def photo_to_operator(update, context, note):
    u = update.effective_user
    msg = update.message
    for aid in ADMIN_IDS:
        try:
            cap = (f"🖼 Rasm — {u.full_name} (@{u.username or '-'}, {u.id})\n{note}\n\n"
                   "Javob berish uchun shu xabarga «Reply» qiling.")
            m = await msg.copy(aid, caption=cap[:1000])
            db.execute("INSERT OR REPLACE INTO op_msgs VALUES(?,?,?)", (aid, m.message_id, u.id))
            db.commit()
        except Exception as e:
            log.warning("Rasm operatorga yuborilmadi: %s", e)


async def photo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u, msg = update.effective_user, update.message
    if track(u) and u.id not in ADMIN_IDS:
        total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        await notify_admins(context, f"🆕 Yangi foydalanuvchi #{total}\n{who(u.id, u.username, u.full_name)}")
    question = (msg.caption or "").strip()
    save_msg(u.id, "user", "[rasm] " + question)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    f = msg.photo[-1] if msg.photo else msg.document
    data = bytes(await (await f.get_file()).download_as_bytearray())

    best = None
    if Image is not None:
        try:
            rows = db.execute("SELECT post, caption, h1, h2 FROM images").fetchall()
            best = await asyncio.to_thread(find_match, data, rows)
        except Exception as e:
            log.warning("Rasm solishtirish xatosi: %s", e)
    if best and best[0] <= MATCH_DIST:
        d, post, cap = best
        cap1 = cap.split("\n")[0][:200] if cap else ""
        ch = post.split("/")[3] if post.count("/") >= 4 else ""
        admin = ("@komil_1247, +998 90 021 12 47" if ch == "Pastel9006" else "@sabaev_admin, +998 90 099 09 44")
        answer = (f"✅ Ha, bu mato bizda bor!\n👉 {post}\n" + (f"📝 {cap1}\n" if cap1 else "") +
                  f"\nNarx va buyurtma uchun: {admin}\nRang/kod va miqdorini (rulon yoki metr) yozsangiz, tayyorlab qo'yamiz 😊")
        save_msg(u.id, "ai", answer)
        await msg.reply_text(answer, disable_web_page_preview=False)
        log.info("Rasm topildi: %s (masofa %d)", post, d)
        return

    answer = ""
    try:
        answer = await vision_reply(u, data, question)
    except Exception as e:
        log.error("Rasm AI xatosi: %s", e)
    if answer:
        answer += "\n\n🔎 Aniq shu rasmni adminimiz tekshirib, tez orada javob beradi."
        save_msg(u.id, "ai", answer)
        await msg.reply_text(answer)
    else:
        await msg.reply_text("🔎 Rasmni oldik! Adminimiz tekshirib, bor-yo'qligini tez orada aytadi 🙏\n"
                             "Katalog: https://claude.ai/artifact/65qGVW5DsKiduNyTDHxFbj")
    near = f"Eng yaqin kanal rasmi: {best[1]} (o'xshashlik masofasi {best[0]})" if best else "Kanal indeksida mos rasm topilmadi"
    await photo_to_operator(update, context, (f"Savol: {question}\n" if question else "") + near)


async def reindex_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    n = db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    if INDEX_STATE["running"]:
        await update.message.reply_text(f"⏳ Indekslash davom etmoqda… hozircha {INDEX_STATE['done']} ta rasm.")
        return
    await update.message.reply_text(f"🔄 Kanallar qayta o'qilmoqda ({', '.join('@' + c for c in CHANNELS)}). "
                                    f"Hozir indeksda {n} ta rasm. Bir necha daqiqa oladi.")
    await build_index()
    n2 = db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    await update.message.reply_text(f"✅ Tayyor: indeksda {n2} ta rasm.")


async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/log — botning oxirgi xatolari (terminal ochmasdan)."""
    if not is_admin(update):
        return
    path = os.path.join(HERE, "bot.log")
    try:
        lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
    except FileNotFoundError:
        await update.message.reply_text("Log fayl yo'q (bot terminaldan ishga tushirilgan).")
        return
    important = [l for l in lines if "ERROR" in l or "WARNING" in l or l.startswith(("✅", "ℹ️", "⚠️", "❌", "🎉"))]
    txt = "\n".join(important[-25:]) or "Xato yo'q ✅"
    await update.message.reply_text("📋 Oxirgi yozuvlar:\n\n" + txt[-3800:])


async def text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    text = (update.message.text or "").strip()
    if not text:
        return
    if u.id in ADMIN_IDS and await admin_reply(update, context):
        return
    if u.id in ADMIN_IDS and CALL_WORDS.search(text):
        phone, raw = find_phone(text)
        name, cphone = (None, None) if phone else find_contact(text)
        if phone or cphone:
            await ask_call(update, context, phone or cphone, name, call_purpose(text, raw, name))
            return
        await update.message.reply_text("Kimga qo'ng'iroq qilay? Raqamni yozing yoki kontakt saqlang: /kontakt Ali +998901234567")
        return
    if track(u) and u.id not in ADMIN_IDS:
        total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        await notify_admins(context, f"🆕 Yangi foydalanuvchi #{total}\n{who(u.id, u.username, u.full_name)}")
    save_msg(u.id, "user", text)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    # 1) Tayyor javob (FAQ) — bepul va aniq
    answer = faq_match(text) or builtin_answer(text, u)
    # 2) Gemini (bepul AI)
    if not answer and GEMINI_KEY:
        try:
            answer = await gemini_reply(u)
        except Exception as e:
            log.error("Gemini xatosi: %s", e)
    # 3) OpenAI (agar kalit bo'lsa)
    if not answer and OPENAI_KEY:
        try:
            answer = await openai_reply(u)
        except Exception as e:
            log.error("OpenAI xatosi: %s", str(e)[:300])
    # 4) Claude (agar kalit bo'lsa)
    if not answer and claude:
        try:
            answer = await claude_reply(u)
        except Exception as e:
            log.error("Claude xatosi: %s", str(e)[:300])
    # 4) Hech biri javob bermasa — operatorga
    if not answer:
        await to_operator(update, context, text)
        return
    save_msg(u.id, "ai", answer)
    for i in range(0, len(answer), 4000):
        await update.message.reply_text(answer[i:i + 4000])


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.execute("DELETE FROM chats WHERE user_id=?", (update.effective_user.id,))
    save_msg(update.effective_user.id, "reset", "")
    db.commit()
    await update.message.reply_text("🔄 Suhbat yangidan boshlandi. Savolingizni yozing.")


# ---------------------------------------------------------------- admin panel
ADMIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="adm:users"),
     InlineKeyboardButton("📞 Suhbatlar", callback_data="adm:calls")],
    [InlineKeyboardButton("💬 Yozishmalar", callback_data="adm:msgs"),
     InlineKeyboardButton("📊 Statistika", callback_data="adm:stats")],
    [InlineKeyboardButton("🔄 Yangilash", callback_data="adm:home")],
])
BACK_KB = [InlineKeyboardButton("⬅️ Orqaga", callback_data="adm:home")]


def home_text():
    total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    new_today = db.execute("SELECT COUNT(*) FROM users WHERE first_seen LIKE ?", (today + "%",)).fetchone()[0]
    return (f"🛠 <b>Admin panel</b>\n\n👥 Foydalanuvchilar: <b>{total}</b> (bugun +{new_today})\n"
            f"🤖 Assistant: {html.escape(ASSISTANT.get('name') or ASSISTANT_ID)}\n\n"
            "Buyruqlar:\n/users — foydalanuvchilar\n/calls — ovozli suhbatlar\n/msgs — yozma xabarlar\n/stats — statistika\n"
            "/assistant — assistantni tanlash\n"
            "/info — AI uchun Sabah ma'lumotlari (kanallar, aloqa, narxlar)\n"
            "/faq — tayyor javoblar (narx, manzil…)\n"
            "/kalit AIza… — bepul Gemini AI ni ulash\n"
            "/log — botdagi xatolarni ko'rish\n"
            "/reindex — kanal rasmlarini qayta o'qish (rasm bo'yicha qidiruv)\n"
            "/send matn — hammaga xabar ({{name}} = mijoz ismi)\n\n"
            "📞 <b>Telefon qo'ng'iroq</b>" + (f" ({html.escape(PHONE_LABEL)})" if PHONE_ID else " — <i>o'chiq, raqam ulanmagan</i>") + "\n"
            "«Aliga ter» yoki «+998901234567 ga qo'ng'iroq qil, buyurtmani tasdiqlasin»\n"
            "/call raqam — qo'ng'iroq\n/kontakt Ali +998... — kontakt saqlash")


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(home_text(), parse_mode="HTML", reply_markup=ADMIN_KB)


def users_text(page=0, per=20):
    total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    rows = db.execute("SELECT id, username, name, first_seen, last_seen, starts FROM users "
                      "ORDER BY last_seen DESC LIMIT ? OFFSET ?", (per, page * per)).fetchall()
    lines = [f"👥 <b>Foydalanuvchilar: {total}</b> (oxirgi faollar)\n"]
    for i, (uid, un, nm, fs, ls, st) in enumerate(rows, page * per + 1):
        lines.append(f"{i}. {who(uid, un, nm)}\n    birinchi: {fmt_time(fs)} · oxirgi: {fmt_time(ls)} · /start×{st}")
    if not rows:
        lines.append("Hali hech kim yo'q.")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"adm:users:{page - 1}"))
    if (page + 1) * per < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"adm:users:{page + 1}"))
    return "\n".join(lines)[:4000], InlineKeyboardMarkup([nav, BACK_KB] if nav else [BACK_KB])


def msgs_text(limit=30):
    rows = db.execute("SELECT m.user_id, m.role, m.text, m.ts, u.username, u.name FROM messages m "
                      "LEFT JOIN users u ON u.id = m.user_id WHERE m.role != 'reset' ORDER BY m.id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "💬 Hali yozma xabarlar yo'q.", InlineKeyboardMarkup([BACK_KB])
    lines = ["💬 <b>Oxirgi yozma xabarlar</b>\n"]
    for uid, role, text, ts, un, nm in reversed(rows):
        t = html.escape(text[:200] + ("…" if len(text) > 200 else ""))
        if role == "user":
            lines.append(f"\n👤 {who(uid, un, nm)} · {fmt_time(ts)}\n{t}")
        else:
            lines.append(f"🤖 <i>{t}</i>")
    out = "\n".join(lines)
    if len(out) > 4000:  # eng yangilarini qoldiramiz, HTML teglarini buzmaslik uchun qator bo'yicha kesamiz
        while len(out) > 4000 and len(lines) > 2:
            lines.pop(1)
            out = "\n".join(lines)
    return out, InlineKeyboardMarkup([BACK_KB])


async def calls_text():
    try:
        calls = await fetch_calls(10)
    except Exception as e:
        return f"Vapi'dan suhbatlarni olib bo'lmadi: {html.escape(str(e))}", InlineKeyboardMarkup([BACK_KB])
    if not calls:
        return "📞 Hali suhbatlar yo'q.", InlineKeyboardMarkup([BACK_KB])
    parts = ["📞 <b>Oxirgi suhbatlar</b>\n"] + [call_line(i, c) for i, c in enumerate(calls, 1)]
    btns = [InlineKeyboardButton(f"📄 {i}", callback_data=f"tr:{c['id']}") for i, c in enumerate(calls, 1)]
    rows = [btns[i:i + 5] for i in range(0, len(btns), 5)] + [BACK_KB]
    return "\n\n".join(parts)[:4000], InlineKeyboardMarkup(rows)


async def stats_text():
    total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    week = (datetime.now(TZ) - timedelta(days=7)).isoformat()
    active7 = db.execute("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (week,)).fetchone()[0]
    try:
        calls = await fetch_calls(100)
    except Exception as e:
        calls, err = [], f"\n(Vapi xatosi: {html.escape(str(e))})"
    else:
        err = ""
    secs = sum(call_seconds(c) for c in calls)
    text_q = db.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0]
    cost = sum(c.get("cost") or 0 for c in calls if isinstance(c.get("cost"), (int, float)))
    today = datetime.now(TZ).strftime("%d.%m")
    calls_today = sum(1 for c in calls if fmt_time(c.get("createdAt")).startswith(today))
    users_called = len({((c.get("assistantOverrides") or {}).get("metadata") or {}).get("tg_id") for c in calls} - {None})
    return (f"📊 <b>Statistika</b>\n\n👥 Jami foydalanuvchi: <b>{total}</b>\n🔥 7 kunda faol: {active7}\n\n"
            f"📞 Suhbatlar (oxirgi {len(calls)} ta): bugun {calls_today}\n"
            f"🗣 Gaplashgan foydalanuvchilar: {users_called}\n"
            f"⏱ Jami vaqt: {secs // 60} daqiqa\n💵 Jami xarajat: ${cost:.2f}\n\n💬 Yozma savollar: {text_q}{err}"), InlineKeyboardMarkup([BACK_KB])


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Ruxsat yo'q", show_alert=True)
        return
    await q.answer()
    parts = q.data.split(":")
    if parts[1] == "users":
        text, kb = users_text(int(parts[2]) if len(parts) > 2 else 0)
    elif parts[1] == "calls":
        text, kb = await calls_text()
    elif parts[1] == "msgs":
        text, kb = msgs_text()
    elif parts[1] == "stats":
        text, kb = await stats_text()
    else:
        text, kb = home_text(), ADMIN_KB
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except Exception:  # matn o'zgarmagan bo'lsa
        pass


async def transcript_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Ruxsat yo'q", show_alert=True)
        return
    await q.answer("Yuklanmoqda…")
    call_id = q.data.split(":", 1)[1]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{VAPI}/call/{call_id}", headers=PRIV_H)
        r.raise_for_status()
        c = r.json()
    except Exception as e:
        await q.message.reply_text(f"Xato: {e}")
        return
    art = c.get("artifact") or {}
    tr = (art.get("transcript") or c.get("transcript") or "").strip() or "Matn yo'q (suhbat juda qisqa yoki hali tugamagan)."
    head = call_line("📄", c)
    if art.get("recordingUrl"):
        head += f'\n🎧 <a href="{html.escape(art["recordingUrl"])}">Yozuvni tinglash</a>'
    await q.message.reply_text(head, parse_mode="HTML", disable_web_page_preview=True)
    for i in range(0, len(tr), 3500):
        await q.message.reply_text(tr[i:i + 3500])


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        text, kb = users_text()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


async def msgs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        text, kb = msgs_text()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


async def calls_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        text, kb = await calls_text()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        text, kb = await stats_text()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    msg = update.message.text.partition(" ")[2].strip()
    if not msg:
        await update.message.reply_text("Foydalanish: /send Xabar matni")
        return
    rows = db.execute("SELECT id, name FROM users").fetchall()
    ids = [r[0] for r in rows]
    ok = 0
    for uid, nm in rows:
        first = (nm or "").split(" ")[0] or "hurmatli mijoz"
        text = re.sub(r"\{\{\s*name\s*\}\}", first, msg)  # {{name}} -> mijoz ismi
        try:
            await context.bot.send_message(uid, text, reply_markup=START_KB)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)  # Telegram limitlari
    await update.message.reply_text(f"📢 Yuborildi: {ok}/{len(ids)}")


# ---------------------------------------------------------------- yangi suhbatlar haqida xabar
async def watch_calls(app):
    first = True
    while True:
        try:
            calls = await fetch_calls(20)
            for c in reversed(calls):
                if c.get("status") != "ended":
                    continue
                if db.execute("SELECT 1 FROM seen_calls WHERE id=?", (c["id"],)).fetchone():
                    continue
                db.execute("INSERT INTO seen_calls VALUES(?)", (c["id"],))
                db.commit()
                if first:
                    continue  # bot ishga tushishidan oldingi suhbatlar haqida xabar bermaymiz
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("📄 To'liq matn", callback_data=f"tr:{c['id']}")]])
                for aid in ADMIN_IDS:
                    try:
                        await app.bot.send_message(aid, "📞 <b>Yangi suhbat tugadi</b>\n" + call_line("•", c),
                                                   parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
                    except Exception as e:
                        log.warning("Adminga yuborilmadi: %s", e)
            first = False
        except Exception as e:
            log.warning("Suhbatlarni tekshirib bo'lmadi: %s", e)
        await asyncio.sleep(60)


async def assistant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{VAPI}/assistant", headers=PRIV_H)
        lst = as_list(r.json())
    except Exception as e:
        await update.message.reply_text(f"Xato: {e}")
        return
    rows = [[InlineKeyboardButton(("✅ " if a["id"] == ASSISTANT_ID else "") + (a.get("name") or a["id"][:8]),
                                  callback_data=f"asst:{a['id']}")] for a in lst]
    await update.message.reply_text("🤖 Qaysi assistant ishlatilsin? (Sabah System Prompt yozilganini tanlang)",
                                    reply_markup=InlineKeyboardMarkup(rows))


async def assistant_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ASSISTANT, ASSISTANT_ID, APP_LINK, START_KB
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Ruxsat yo'q", show_alert=True)
        return
    aid = q.data.split(":", 1)[1]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{VAPI}/assistant/{aid}", headers=PRIV_H)
    if r.status_code != 200:
        await q.answer("Topilmadi", show_alert=True)
        return
    ASSISTANT, ASSISTANT_ID = r.json(), aid
    APP_LINK, START_KB = build_start_kb()
    txt = open(ENV_FILE).read()
    txt = re.sub(r"(?m)^VAPI_ASSISTANT_ID=.*$", f"VAPI_ASSISTANT_ID={aid}", txt)
    open(ENV_FILE, "w").write(txt)
    await q.answer("Saqlandi")
    await q.edit_message_text(f"✅ Endi ishlatiladigan assistant: {ASSISTANT.get('name') or aid}\n"
                              "Mijozlar /start bosib yangi tugmani olishadi.")


async def post_init(app):
    app.bot_data["watch_task"] = asyncio.create_task(watch_calls(app))
    app.bot_data["index_task"] = asyncio.create_task(index_loop(app))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import Conflict, NetworkError
    if isinstance(context.error, Conflict):
        log.error("⚠️ Bu bot boshqa joyda ham ishlayapti (boshqa terminal oynasi?). O'sha oynani yoping.")
        return
    if isinstance(context.error, NetworkError):
        log.warning("Internet uzildi, qayta ulanmoqda… (%s)", context.error)
        return
    log.error("Kutilmagan xato", exc_info=context.error)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("calls", calls_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("send", send_cmd))
    app.add_handler(CommandHandler("msgs", msgs_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("faq", faq_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("log", log_cmd))
    app.add_handler(CommandHandler(["kalit", "key"], key_cmd))
    app.add_handler(CommandHandler("call", call_cmd))
    app.add_handler(CommandHandler("assistant", assistant_cmd))
    app.add_handler(CallbackQueryHandler(assistant_cb, pattern="^asst:"))
    app.add_handler(CommandHandler(["kontakt", "contact"], contact_cmd))
    app.add_handler(CallbackQueryHandler(dial_cb, pattern="^(no)?dial:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_msg))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & filters.ChatType.PRIVATE, photo_msg))
    app.add_handler(CommandHandler("reindex", reindex_cmd))
    app.add_handler(CallbackQueryHandler(talk, pattern="^talk$"))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern="^adm:"))
    app.add_handler(CallbackQueryHandler(transcript_cb, pattern="^tr:"))
    app.add_error_handler(on_error)
    print(f"\n🎉 Bot ishlayapti! Telegram'da @{BOT_NAME} ga /start yozing. Admin panel: /admin  (To'xtatish: Ctrl+C)\n")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
