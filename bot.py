import os
import re
import logging
from datetime import datetime
from io import BytesIO

from PIL import Image
import pytesseract
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, ContextTypes, filters
)

import database as db

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# temp holding area for a payment entry being confirmed: {admin_id: {...}}
PENDING = {}


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# /start and /addpayment (format helper)
# ---------------------------------------------------------------------------

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot active hai.\n\n"
        "Available commands:\n"
        "/addpayment — payment entry ka format dekhne ke liye\n"
        "/check <user_id> — user ki complete details\n"
        "/channels — sabhi channels ki list"
    )


@admin_only
async def addpayment_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Payment entry add karne ke liye payment screenshot bhejein, caption mein niche diya format use karein:\n\n"
        "Name: Rahul Sharma\n"
        "Username: @rahul123\n"
        "UserID: 123456789\n"
        "Course: Digital Marketing Batch 2\n\n"
        "Amount aur date screenshot se automatically detect kiye jayenge. Confirmation ke baad entry save ho jayegi."
    )


# ---------------------------------------------------------------------------
# Photo + caption -> OCR -> confirm -> save
# ---------------------------------------------------------------------------

def parse_caption(caption: str):
    data = {}
    patterns = {
        "name": r"name\s*:\s*(.+)",
        "username": r"username\s*:\s*(.+)",
        "user_id": r"user\s*id\s*:\s*(\d+)",
        "course": r"course\s*:\s*(.+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, caption, re.IGNORECASE)
        if m:
            data[key] = m.group(1).strip()
    return data


def extract_amount_and_date(ocr_text: str):
    amount = None
    amt_match = re.search(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", ocr_text, re.IGNORECASE)
    if amt_match:
        amount = amt_match.group(1).replace(",", "")
    date_val = None
    date_match = re.search(
        r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})",
        ocr_text
    )
    if date_match:
        date_val = date_match.group(1)
    return amount, date_val


@admin_only
async def handle_payment_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    parsed = parse_caption(caption)

    missing = [k for k in ("name", "username", "user_id", "course") if k not in parsed]
    if missing:
        await update.message.reply_text(
            f"Caption mein ye fields missing hain: {', '.join(missing)}\n"
            "Sahi format ke liye /addpayment check karein."
        )
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    buf = BytesIO()
    await file.download_to_memory(out=buf)
    buf.seek(0)

    try:
        img = Image.open(buf)
        ocr_text = pytesseract.image_to_string(img)
    except Exception as e:
        logger.error(f"OCR failed: {e}")
        ocr_text = ""

    amount, date_val = extract_amount_and_date(ocr_text)

    PENDING[update.effective_user.id] = {
        "name": parsed["name"],
        "username": parsed["username"],
        "user_id": int(parsed["user_id"]),
        "course": parsed["course"],
        "amount": amount,
        "date": date_val,
        "screenshot_file_id": photo.file_id,
    }

    amount_display = amount if amount else "❓ Detect nahi hua, manually set karein"
    date_display = date_val if date_val else "❓ Detect nahi hua, manually set karein (YYYY-MM-DD)"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Save", callback_data="confirm_payment")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_payment")],
    ])

    await update.message.reply_text(
        f"📋 Entry Preview\n\n"
        f"Name: {parsed['name']}\n"
        f"Username: {parsed['username']}\n"
        f"UserID: {parsed['user_id']}\n"
        f"Course: {parsed['course']}\n"
        f"Amount: {amount_display}\n"
        f"Date: {date_display}\n\n"
        f"Agar amount ya date incorrect hai, confirm karne se pehle correct karein:\n"
        f"`/fixamount 1499` ya `/fixdate 2026-01-12`",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@admin_only
async def fix_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = PENDING.get(update.effective_user.id)
    if not pending:
        await update.message.reply_text("Koi pending entry nahi hai.")
        return
    if not context.args:
        await update.message.reply_text("Sahi format: /fixamount 1499")
        return
    pending["amount"] = context.args[0]
    await update.message.reply_text(f"Amount update kar diya gaya hai: {pending['amount']}")


@admin_only
async def fix_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = PENDING.get(update.effective_user.id)
    if not pending:
        await update.message.reply_text("Koi pending entry nahi hai.")
        return
    if not context.args:
        await update.message.reply_text("Sahi format: /fixdate 2026-01-12")
        return
    pending["date"] = context.args[0]
    await update.message.reply_text(f"Date update kar diya gaya hai: {pending['date']}")


@admin_only
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pending = PENDING.get(update.effective_user.id)

    if query.data == "cancel_payment":
        PENDING.pop(update.effective_user.id, None)
        await query.edit_message_text("❌ Entry cancel kar di gayi hai.")
        return

    if query.data == "confirm_payment":
        if not pending:
            await query.edit_message_text("Ye entry expire ho chuki hai, kripya screenshot dobara bhejein.")
            return
        if not pending.get("amount") or not pending.get("date"):
            await query.edit_message_text(
                "Amount ya date abhi bhi missing hai. Pehle /fixamount aur /fixdate se set karein, "
                "phir dobara Confirm button press karein."
            )
            return
        try:
            purchase_date = parse_flexible_date(pending["date"])
        except Exception:
            await query.edit_message_text("Date sahi format mein nahi hai. Kripya /fixdate 2026-01-12 format use karein.")
            return

        db.add_purchase(
            user_id=pending["user_id"],
            name=pending["name"],
            username=pending["username"],
            course_name=pending["course"],
            amount=pending["amount"],
            purchase_date=purchase_date,
            screenshot_file_id=pending["screenshot_file_id"],
        )
        PENDING.pop(update.effective_user.id, None)
        await query.edit_message_text("✅ Entry safaltapoorvak save ho gayi hai.")


def parse_flexible_date(raw: str):
    raw = raw.strip()
    formats = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%d/%m/%y"]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {raw}")


# ---------------------------------------------------------------------------
# /check <user_id> - full bio
# ---------------------------------------------------------------------------

@admin_only
async def check_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Sahi format: /check 123456789")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User ID valid number hona chahiye.")
        return

    purchases = db.get_purchases(user_id)
    channels = db.get_user_channels(user_id)

    if not purchases and not channels:
        await update.message.reply_text("Is User ID ke liye koi record nahi mila.")
        return

    current_name = None
    current_username = None
    try:
        chat = await context.bot.get_chat(user_id)
        current_name = " ".join(filter(None, [chat.first_name, chat.last_name]))
        current_username = f"@{chat.username}" if chat.username else "(no username)"
    except Exception:
        pass

    lines = [f"👤 User ID: {user_id}"]

    if purchases:
        purchase_name = purchases[0]["name_at_purchase"]
        purchase_username = purchases[0]["username_at_purchase"]
        lines.append(f"Name (purchase ke samay): {purchase_name}")
        if current_name and current_name != purchase_name:
            lines.append(f"Name (current): {current_name} ⚠️ (change ho chuka hai)")
        elif current_name:
            lines.append(f"Name (current): {current_name} (unchanged)")
        lines.append(f"Username (purchase ke samay): {purchase_username}")
        if current_username:
            lines.append(f"Username (current): {current_username}")
    elif current_name:
        lines.append(f"Name: {current_name}")
        lines.append(f"Username: {current_username}")

    lines.append("")
    if purchases:
        lines.append("📚 Purchase History:")
        for p in purchases:
            lines.append(f"  • {p['course_name']} — ₹{p['amount']} — {p['purchase_date']}")
    else:
        lines.append("📚 Koi purchase record uplabdh nahi hai.")

    lines.append("")
    if channels:
        lines.append("📢 Active Channels:")
        for c in channels:
            joined = c["joined_at"].strftime("%d %b %Y") if c["joined_at"] else "—"
            link = c["invite_link"] or "(link uplabdh nahi hai)"
            lines.append(f"  • {c['title']} — joined on {joined}\n    {link}")
    else:
        lines.append("📢 User is samay kisi bhi channel mein active nahi hai.")

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /channels - list all, tap to see members
# ---------------------------------------------------------------------------

@admin_only
async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channels = db.get_all_channels()
    if not channels:
        await update.message.reply_text(
            "Abhi tak koi channel register nahi hua hai. Bot ko us channel mein Administrator banayein — "
            "ek member ke join/leave hote hi channel automatically register ho jayega."
        )
        return
    keyboard = [
        [InlineKeyboardButton(c["title"] or str(c["channel_id"]), callback_data=f"chinfo_{c['channel_id']}")]
        for c in channels
    ]
    await update.message.reply_text(
        "📢 Registered Channels:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


@admin_only
async def channel_info_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = int(query.data.replace("chinfo_", ""))
    members = db.get_channel_members(channel_id)

    channels = {c["channel_id"]: c for c in db.get_all_channels()}
    title = channels.get(channel_id, {}).get("title", str(channel_id))

    if not members:
        await query.edit_message_text(f"📢 {title}\nIs channel mein abhi koi active member nahi hai.")
        return

    lines = [f"📢 {title}", f"Total Active Members: {len(members)}", ""]
    for m in members:
        joined = m["joined_at"].strftime("%d %b %Y") if m["joined_at"] else "—"
        lines.append(f"  • UserID: {m['user_id']} — joined on {joined}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n...(list truncated hai)"
    await query.edit_message_text(text)


# ---------------------------------------------------------------------------
# Channel membership tracking (bot must be admin in the channel)
# ---------------------------------------------------------------------------

async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if result is None:
        return

    chat = result.chat
    user = result.new_chat_member.user
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    invite_link = None
    try:
        invite_link = await context.bot.export_chat_invite_link(chat.id)
    except Exception:
        pass
    db.register_channel(chat.id, chat.title, invite_link)

    was_member = old_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    is_member = new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

    if not was_member and is_member:
        db.log_join(user.id, chat.id)
    elif was_member and not is_member:
        db.log_leave(user.id, chat.id)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

def main():
    db.init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addpayment", addpayment_help))
    app.add_handler(CommandHandler("check", check_user))
    app.add_handler(CommandHandler("channels", list_channels))
    app.add_handler(CommandHandler("fixamount", fix_amount))
    app.add_handler(CommandHandler("fixdate", fix_date))

    app.add_handler(MessageHandler(filters.PHOTO & filters.CAPTION, handle_payment_photo))
    app.add_handler(CallbackQueryHandler(channel_info_button, pattern=r"^chinfo_"))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(confirm_payment|cancel_payment)$"))

    app.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.CHAT_MEMBER))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
