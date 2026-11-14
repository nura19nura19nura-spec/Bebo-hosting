
import os
import sqlite3
import asyncio
import signal
import sys
import subprocess
from pathlib import Path
from uuid import uuid4

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN", "9162956375:AAFrPLjKQT1utjC0p134H1z3PYFmO0TI1os")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

BASE = Path("data")
UPLOADS = BASE / "uploads"
LOGS = BASE / "logs"

BASE.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)

db = sqlite3.connect(BASE / "hosting.db", check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS bots (
 id TEXT PRIMARY KEY,
 user_id INTEGER,
 filename TEXT,
 status TEXT DEFAULT 'pending',
 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
db.commit()

processes = {}


def is_owner(user_id):
    return user_id == OWNER_ID


def get_bot(bot_id):
    return db.execute(
        "SELECT id,user_id,filename,status FROM bots WHERE id=?",
        (bot_id,)
    ).fetchone()


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 رفع بوت", callback_data="upload")],
        [InlineKeyboardButton("📦 بوتاتي", callback_data="mybots")],
        [InlineKeyboardButton("👑 لوحة الإدارة", callback_data="admin")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً بك في بيبو للاستضافة 🤖\n\n"
        "ارسل ملف Python بصيغة .py لرفعه ومراجعته.",
        reply_markup=main_menu()
    )


async def upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document

    if not doc.file_name or not doc.file_name.endswith(".py"):
        await update.message.reply_text("❌ ارسل ملف Python بصيغة .py فقط.")
        return

    if doc.file_size > 1024 * 1024:
        await update.message.reply_text("❌ الحد الأقصى للملف 1 ميجابايت.")
        return

    bot_id = uuid4().hex[:10]
    filename = f"{bot_id}.py"
    path = UPLOADS / filename

    file = await context.bot.get_file(doc.file_id)
    await file.download_to_drive(custom_path=str(path))

    db.execute(
        "INSERT INTO bots(id,user_id,filename,status) VALUES(?,?,?,'pending')",
        (bot_id, update.effective_user.id, filename)
    )
    db.commit()

    await update.message.reply_text(
        f"✅ تم استلام الملف.\nرقم الطلب: {bot_id}\n"
        "سيتم مراجعته من الإدارة قبل التشغيل."
    )

    if OWNER_ID:
        await context.bot.send_message(
            OWNER_ID,
            f"📥 طلب استضافة جديد\n"
            f"المستخدم: {update.effective_user.id}\n"
            f"الملف: {filename}\n"
            f"رقم الطلب: {bot_id}\n\n"
            f"/approve {bot_id}\n"
            f"/run {bot_id}\n"
            f"/reject {bot_id}"
        )


async def mybots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.execute(
        "SELECT id,filename,status FROM bots WHERE user_id=?",
        (update.effective_user.id,)
    ).fetchall()

    if not rows:
        await update.message.reply_text("ليس لديك ملفات مرفوعة.")
        return

    result = "\n".join(
        f"📦 {r[1]} | {r[0]} | {r[2]}" for r in rows
    )
    await update.message.reply_text(result)


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("الاستخدام: /approve رقم_الطلب")
        return

    bot_id = context.args[0]
    row = get_bot(bot_id)

    if not row:
        await update.message.reply_text("الطلب غير موجود.")
        return

    db.execute("UPDATE bots SET status='approved' WHERE id=?", (bot_id,))
    db.commit()
    await update.message.reply_text(f"✅ تمت الموافقة على {bot_id}")


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    if not context.args:
        return

    bot_id = context.args[0]
    row = get_bot(bot_id)

    if not row:
        await update.message.reply_text("الطلب غير موجود.")
        return

    if bot_id in processes:
        await stop_process(bot_id)

    db.execute("UPDATE bots SET status='rejected' WHERE id=?", (bot_id,))
    db.commit()

    path = UPLOADS / row[2]
    if path.exists():
        path.unlink()

    await update.message.reply_text("تم رفض الطلب وحذف الملف.")


async def run_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("الاستخدام: /run رقم_الطلب")
        return

    bot_id = context.args[0]
    row = get_bot(bot_id)

    if not row:
        await update.message.reply_text("الملف غير موجود.")
        return

    if row[3] != "approved":
        await update.message.reply_text("يجب الموافقة على الملف أولاً.")
        return

    if bot_id in processes and processes[bot_id].poll() is None:
        await update.message.reply_text("البوت يعمل بالفعل.")
        return

    path = (UPLOADS / row[2]).resolve()

    if not path.is_file() or path.parent != UPLOADS.resolve():
        await update.message.reply_text("مسار الملف غير صالح.")
        return

    log_path = LOGS / f"{bot_id}.log"

    try:
        log_file = open(log_path, "ab")
        process = subprocess.Popen(
            [sys.executable, "-I", str(path)],
            cwd=str(UPLOADS.resolve()),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
        processes[bot_id] = process
        db.execute("UPDATE bots SET status='running' WHERE id=?", (bot_id,))
        db.commit()
        await update.message.reply_text(
            f"🚀 بدأ تشغيل البوت.\nرقم العملية: {process.pid}"
        )
    except Exception as exc:
        await update.message.reply_text(f"تعذر التشغيل: {exc}")


async def stop_process(bot_id):
    process = processes.get(bot_id)

    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    processes.pop(bot_id, None)

    db.execute("UPDATE bots SET status='approved' WHERE id=?", (bot_id,))
    db.commit()


async def stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("الاستخدام: /stopbot رقم_الطلب")
        return

    bot_id = context.args[0]

    if not get_bot(bot_id):
        await update.message.reply_text("الطلب غير موجود.")
        return

    await stop_process(bot_id)
    await update.message.reply_text("تم إيقاف البوت.")


async def list_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    rows = db.execute(
        "SELECT id,filename,status FROM bots ORDER BY created_at DESC"
    ).fetchall()

    if not rows:
        await update.message.reply_text("لا توجد بوتات.")
        return

    text = "\n".join(
        f"{r[0]} | {r[1]} | {r[2]}" for r in rows
    )
    await update.message.reply_text(text[:4000])


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "upload":
        await query.message.reply_text("ارسل ملف Python بصيغة .py")
    elif query.data == "mybots":
        rows = db.execute(
            "SELECT id,filename,status FROM bots WHERE user_id=?",
            (query.from_user.id,)
        ).fetchall()
        text = "\n".join(f"{r[1]} | {r[0]} | {r[2]}" for r in rows)
        await query.message.reply_text(text or "ليس لديك ملفات.")
    elif query.data == "admin":
        if is_owner(query.from_user.id):
            await query.message.reply_text(
                "👑 لوحة الإدارة\n"
                "/bots - عرض الملفات\n"
                "/approve ID - الموافقة\n"
                "/run ID - التشغيل\n"
                "/stopbot ID - الإيقاف\n"
                "/reject ID - الرفض"
            )


async def error_handler(update, context):
    print("Telegram error:", context.error)


def main():
    if not TOKEN or OWNER_ID == 0:
        raise RuntimeError("اضبط BOT_TOKEN و OWNER_ID في Environment Variables")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bots", list_bots))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("run", run_bot))
    app.add_handler(CommandHandler("stopbot", stop_bot))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, upload_file))
    app.add_error_handler(error_handler)

    print("Bebo Hosting is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
