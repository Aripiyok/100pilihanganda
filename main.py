import os
import json
import logging
from datetime import datetime, time, timezone
from typing import Dict, Any, Set

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from dotenv import load_dotenv

# ===================== LOGGING =====================
logging.basicConfig(
    format='[%(asctime)s] %(levelname)s - %(message)s',
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ===================== ENV =========================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    log.error("❌ BOT_TOKEN tidak ditemukan di .env")

# ================== STORAGE ========================
SCORES_FILE = "scores.json"


def current_period_str() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def load_scores() -> Dict[str, Any]:
    if not os.path.exists(SCORES_FILE):
        return {"period": current_period_str(), "groups": {}}
    try:
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"⚠️ Gagal baca {SCORES_FILE}: {e}")
        return {"period": current_period_str(), "groups": {}}


def save_scores():
    try:
        with open(SCORES_FILE, "w", encoding="utf-8") as f:
            json.dump(scores, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"❌ Gagal simpan {SCORES_FILE}: {e}")


scores = load_scores()  # global


def ensure_group(chat_id: int):
    gid = str(chat_id)
    if gid not in scores["groups"]:
        scores["groups"][gid] = {"users": {}, "names": {}}


def add_score(chat_id: int, user_id: int, display: str, delta: int = 1):
    gid = str(chat_id)
    uid = str(user_id)
    ensure_group(chat_id)
    grp = scores["groups"][gid]
    grp["names"][uid] = display
    grp["users"][uid] = grp["users"].get(uid, 0) + delta
    save_scores()


def top10_text(chat_id: int) -> str:
    gid = str(chat_id)
    if gid not in scores["groups"] or not scores["groups"][gid]["users"]:
        return "📊 Belum ada skor untuk bulan ini."
    users = scores["groups"][gid]["users"]
    names = scores["groups"][gid].get("names", {})
    ranking = sorted(users.items(), key=lambda kv: (-kv[1], names.get(kv[0], "")))[:10]
    lines = ["🏆 *TOP 10 BULAN INI*"]
    for i, (uid, pts) in enumerate(ranking, start=1):
        disp = names.get(uid, f"Player {uid}")
        lines.append(f"{i}. {disp} — {pts} poin")
    return "\n".join(lines)


def reset_month_if_needed():
    current = current_period_str()
    if scores.get("period") != current:
        log.info(f"🔁 Reset bulanan: {scores.get('period')} → {current}")
        scores["period"] = current
        scores["groups"] = {}
        save_scores()


# ================== DATA GAME ======================
rooms: Dict[int, Dict[str, Any]] = {}
questions = []


# ================== LOAD QUESTIONS =================
def load_questions_txt(filepath="soal.txt"):
    q = []
    if not os.path.exists(filepath):
        log.error("❌ soal.txt tidak ditemukan!")
        return q
    with open(filepath, "r", encoding="utf-8") as f:
        blocks = f.read().strip().split("---")

    nomor = 0
    for block in blocks:
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        if not lines:
            continue
        nomor += 1
        if len(lines) < 6:
            log.warning(f"⚠️ Blok soal ke-{nomor} kurang baris, dilewati.")
            continue
        qtext = lines[0]
        opts = lines[1:5]
        benar_raw = ""
        for line in lines[5:]:
            if line.upper().startswith("BENAR="):
                benar_raw = line.split("=", 1)[1].strip()
                break
        benar = (
            benar_raw.replace("—", "")
            .replace("–", "")
            .replace("-", "")
            .replace(" ", "")
            .upper()
        )
        idx_map = {"A": 0, "B": 1, "C": 2, "D": 3}
        if benar not in idx_map:
            log.error(f"❌ Format BENAR= salah di soal ke-{nomor}: '{benar_raw}'")
            continue
        q.append({"q": qtext, "options": opts, "answer": idx_map[benar]})
    log.info(f"✅ {len(q)} soal berhasil dimuat.")
    return q


# ================== KEYBOARD 2×2 ===================
def build_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("A", callback_data=f"ans|{chat_id}|0"),
            InlineKeyboardButton("B", callback_data=f"ans|{chat_id}|1"),
        ],
        [
            InlineKeyboardButton("C", callback_data=f"ans|{chat_id}|2"),
            InlineKeyboardButton("D", callback_data=f"ans|{chat_id}|3"),
        ],
    ])


# ==================== HELPERS ======================
async def send_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    room = rooms.get(chat_id)
    if not room:
        return
    idx = room["current_q"]
    if idx >= len(questions):
        await context.bot.send_message(chat_id=chat_id, text="🎉 *Kuis selesai!*", parse_mode="Markdown")
        return
    room["answered"] = set()
    room["solved"] = False
    q = questions[idx]
    text = (
        f"❓ *Soal {idx+1}*\n"
        f"{q['q']}\n\n"
        f"A. {q['options'][0]}\n"
        f"B. {q['options'][1]}\n"
        f"C. {q['options'][2]}\n"
        f"D. {q['options'][3]}"
    )
    msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=build_keyboard(chat_id))
    room["active_msg_id"] = msg.message_id


async def lock_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    room = rooms.get(chat_id)
    if not room or not room.get("active_msg_id"):
        return
    try:
        await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=room["active_msg_id"], reply_markup=None)
    except Exception as e:
        log.debug(f"Gagal hapus keyboard: {e}")


def display_name(user) -> str:
    return f"@{user.username}" if user.username else user.first_name


# ==================== COMMANDS =====================
async def host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Game hanya untuk *grup*.", parse_mode="Markdown")
        return
    chat_id = update.effective_chat.id
    rooms[chat_id] = {"host": update.effective_user.id, "players": set(), "current_q": 0, "answered": set(), "solved": False, "active_msg_id": None}
    await update.message.reply_text("✅ Room dibuat!\nPemain ketik */gabung*.\nHost jalankan */startgame*.", parse_mode="Markdown")


async def gabung(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    room = rooms.get(chat_id)
    if not room:
        await update.message.reply_text("❌ Belum ada room. Host jalankan /host.")
        return
    room["players"].add(update.effective_user.id)
    ensure_group(chat_id)
    add_score(chat_id, update.effective_user.id, display_name(update.effective_user), 0)
    await update.message.reply_text(f"✅ {update.effective_user.first_name} bergabung!")


async def startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    room = rooms.get(chat_id)
    if not room:
        await update.message.reply_text("❌ Belum ada room. Jalankan /host.")
        return
    if room["host"] != update.effective_user.id:
        await update.message.reply_text("❌ Hanya host yang bisa memulai.")
        return
    room["current_q"] = 0
    await update.message.reply_text("🎮 Kuis dimulai! Siap-siap adu cepat!")
    await send_question(context, chat_id)


async def juara(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_group(chat_id)
    txt = top10_text(chat_id)
    await update.message.reply_text(txt, parse_mode="Markdown")


# ================= CALLBACK HANDLER =================
async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer(cache_time=0)
    except:
        pass
    data = query.data or ""
    if not data.startswith("ans|"):
        return
    _, cb_chat, sel = data.split("|")
    cb_chat = int(cb_chat)
    selected = int(sel)
    chat_id = update.effective_chat.id
    user = update.effective_user
    if chat_id != cb_chat:
        return
    room = rooms.get(chat_id)
    if not room:
        return
    if user.id not in room["players"]:
        await query.answer("❗ Kamu belum /gabung.", show_alert=False)
        return
    if room["solved"]:
        return
    if user.id in room["answered"]:
        return
    room["answered"].add(user.id)
    q = questions[room["current_q"]]
    correct_idx = q["answer"]
    if selected == correct_idx:
        name = display_name(user)
        ensure_group(chat_id)
        add_score(chat_id, user.id, name, 1)
        label = "ABCD"[selected]
        await context.bot.send_message(chat_id=chat_id, text=f"🎉 *Pemenang tercepat:* {name} — *Jawaban:* {label}*", parse_mode="Markdown")
        room["solved"] = True
        await lock_keyboard(context, chat_id)
        room["current_q"] += 1
        await send_question(context, chat_id)
    else:
        await query.answer("❌ Salah! Tunggu soal berikutnya.", show_alert=False)


# ================= RESET BULANAN ====================
async def monthly_reset_job(context: ContextTypes.DEFAULT_TYPE):
    reset_month_if_needed()


# ===================== MAIN ========================
def main():
    global questions
    questions = load_questions_txt()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    job_queue = app.job_queue

    app.add_handler(CommandHandler("host", host))
    app.add_handler(CommandHandler("gabung", gabung))
    app.add_handler(CommandHandler("startgame", startgame))
    app.add_handler(CommandHandler("juara", juara))
    app.add_handler(CallbackQueryHandler(answer))

    if job_queue:
        job_queue.run_monthly(monthly_reset_job, when=time(0, 0, 0, tzinfo=timezone.utc), day=1, name="monthly_reset_scores")

    log.info("✅ Bot siap! Tambahkan ke grup dan disable privacy di BotFather (/setprivacy → Disable).")
    app.run_polling()


if __name__ == "__main__":
    main()
