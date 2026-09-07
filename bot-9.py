import os
import re
import logging
from datetime import datetime
from io import BytesIO

from PIL import Image
import pytesseract
from fpdf import FPDF
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, ConversationHandler, ContextTypes, filters
)

import database as db

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ROOT_ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "Course Payment Receipt")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DIVIDER = "─" * 24

# temp holding area for a payment entry awaiting confirmation: {admin_id: {...}}
PENDING = {}

# Conversation states for /addpayment
ASK_NAME, ASK_USERNAME, ASK_USERID, ASK_COURSE, ASK_SCREENSHOT = range(5)


def is_authorized(user_id: int) -> bool:
    if user_id == ROOT_ADMIN_ID:
        return True
    return db.is_admin_in_db(user_id)


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update.effective_user.id):
            return
        return await func(update, context)
    return wrapper


def root_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ROOT_ADMIN_ID:
            await update.message.reply_text(
                "⛔ Ye command sirf *Primary Admin* use kar sakta hai.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_root = update.effective_user.id == ROOT_ADMIN_ID
    lines = [
        f"🎓 *{BUSINESS_NAME}*",
        DIVIDER,
        "*Payment Management:*",
        "  /addpayment — Nayi payment entry add karein",
        "  /check `<user_id>` — Student ki complete profile dekhein",
        "  /channels — Sabhi channels ki list",
        "  /cancel — Chal rahi process cancel karein",
    ]
    if is_root:
        lines += [
            "",
            "*Admin Management* _(Primary Admin only)_:",
            "  /addadmin `<user_id>` — Naya admin add karein",
            "  /removeadmin `<user_id>` — Admin access remove karein",
            "  /listadmins — Sabhi admins ki list",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Admin management
# ---------------------------------------------------------------------------

@admin_only
async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Sahi format: `/addadmin 123456789`", parse_mode=ParseMode.MARKDOWN
        )
        return
    new_id = int(context.args[0])
    if new_id == ROOT_ADMIN_ID or db.is_admin_in_db(new_id):
        await update.message.reply_text("Ye user pehle se hi admin access rakhta hai.")
        return
    db.add_admin(new_id, added_by=update.effective_user.id)

    display_name = str(new_id)
    try:
        chat = await context.bot.get_chat(new_id)
        display_name = f"{chat.first_name or ''} {chat.last_name or ''}".strip() or str(new_id)
    except Exception:
        pass

    await update.message.reply_text(
        f"✅ *Naya Admin Add Kiya Gaya*\n{DIVIDER}\n"
        f"Naam: {display_name}\nUser ID: `{new_id}`\n\n"
        f"Is user ke paas ab wahi access hai jo aapke paas hai — /addpayment, /check, /channels, sab kuch.",
        parse_mode=ParseMode.MARKDOWN
    )


@admin_only
@root_only
async def remove_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Sahi format: `/removeadmin 123456789`", parse_mode=ParseMode.MARKDOWN
        )
        return
    target_id = int(context.args[0])
    if target_id == ROOT_ADMIN_ID:
        await update.message.reply_text("Primary Admin ko remove nahi kiya ja sakta.")
        return
    removed = db.remove_admin(target_id)
    if removed:
        await update.message.reply_text(f"✅ User `{target_id}` ka admin access remove kar diya gaya hai.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("Ye user admin list mein mila nahi.")


@admin_only
async def list_admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = db.get_all_admins()
    lines = [f"👑 *Admin List*", DIVIDER, f"• `{ROOT_ADMIN_ID}` — Primary Admin"]
    for a in admins:
        added = a["added_at"].strftime("%d %b %Y") if a.get("added_at") else "—"
        lines.append(f"• `{a['_id']}` — added on {added}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# /addpayment - step-by-step conversation
# ---------------------------------------------------------------------------

async def addpayment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        f"📝 *Nayi Payment Entry*\n{DIVIDER}\nStudent ka naam bataiye:",
        parse_mode=ParseMode.MARKDOWN
    )
    return ASK_NAME


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("Username bataiye (@username):")
    return ASK_USERNAME


async def ask_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["username"] = update.message.text.strip()
    await update.message.reply_text("User ID bataiye (numeric Telegram ID):")
    return ASK_USERID


async def ask_userid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("⚠️ User ID sirf numbers mein hona chahiye. Kripya dobara bhejein:")
        return ASK_USERID
    context.user_data["user_id"] = int(text)
    await update.message.reply_text("Course ya batch ka naam bataiye:")
    return ASK_COURSE


async def ask_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["course"] = update.message.text.strip()
    await update.message.reply_text("📸 Ab payment screenshot bhejein.")
    return ASK_SCREENSHOT


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


async def ask_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("⚠️ Kripya payment screenshot bhejein (image format mein).")
        return ASK_SCREENSHOT

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
    data = context.user_data

    PENDING[update.effective_user.id] = {
        "name": data["name"],
        "username": data["username"],
        "user_id": data["user_id"],
        "course": data["course"],
        "amount": amount,
        "date": date_val,
        "screenshot_file_id": photo.file_id,
    }

    amount_display = f"₹{amount}" if amount else "❓ Detect nahi hua — /fixamount se set karein"
    date_display = date_val if date_val else "❓ Detect nahi hua — /fixdate se set karein"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Save", callback_data="confirm_payment")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_payment")],
    ])

    await update.message.reply_text(
        f"📋 *Entry Preview*\n{DIVIDER}\n"
        f"*Name:* {data['name']}\n"
        f"*Username:* {data['username']}\n"
        f"*User ID:* `{data['user_id']}`\n"
        f"*Course:* {data['course']}\n"
        f"*Amount:* {amount_display}\n"
        f"*Date:* {date_display}\n\n"
        f"_Amount ya date galat ho to confirm se pehle:_\n"
        f"`/fixamount 1499` ya `/fixdate 2026-01-12`",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Process cancel kar diya gaya hai.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /fixamount and /fixdate - correct OCR misreads before confirming
# ---------------------------------------------------------------------------

@admin_only
async def fix_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = PENDING.get(update.effective_user.id)
    if not pending:
        await update.message.reply_text("Koi pending entry nahi hai.")
        return
    if not context.args:
        await update.message.reply_text("Sahi format: `/fixamount 1499`", parse_mode=ParseMode.MARKDOWN)
        return
    pending["amount"] = context.args[0]
    await update.message.reply_text(f"✅ Amount update ho gaya: ₹{pending['amount']}")


@admin_only
async def fix_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = PENDING.get(update.effective_user.id)
    if not pending:
        await update.message.reply_text("Koi pending entry nahi hai.")
        return
    if not context.args:
        await update.message.reply_text("Sahi format: `/fixdate 2026-01-12`", parse_mode=ParseMode.MARKDOWN)
        return
    pending["date"] = context.args[0]
    await update.message.reply_text(f"✅ Date update ho gaya: {pending['date']}")


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
# PDF Receipt generation
# ---------------------------------------------------------------------------

def generate_receipt_pdf(receipt_no, data, purchase_date):
    pdf = FPDF()
    pdf.add_page()

    pdf.set_fill_color(30, 30, 40)
    pdf.rect(0, 0, 210, 30, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_xy(0, 8)
    pdf.cell(210, 10, BUSINESS_NAME, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.set_xy(0, 18)
    pdf.cell(210, 8, "Official Payment Receipt", align="C")

    pdf.set_text_color(0, 0, 0)
    pdf.set_y(40)

    pdf.set_font("Helvetica", "", 11)
    rows = [
        ("Receipt No.", receipt_no),
        ("Date", str(purchase_date)),
        ("Student Name", data["name"]),
        ("Username", data["username"]),
        ("User ID", str(data["user_id"])),
        ("Course", data["course"]),
        ("Amount Paid", f"Rs. {data['amount']}"),
    ]
    fill = False
    for label, value in rows:
        pdf.set_fill_color(245, 245, 245)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(55, 10, f"  {label}", border=0, fill=fill)
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 10, str(value), ln=True, fill=fill)
        fill = not fill

    pdf.ln(10)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 6, "This is a system-generated receipt confirming the payment recorded above. "
                          "Please retain this for your records.")

    filename = f"/tmp/receipt_{data['user_id']}_{receipt_no}.pdf"
    pdf.output(filename)
    return filename


# ---------------------------------------------------------------------------
# Confirm / Cancel buttons
# ---------------------------------------------------------------------------

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
            await query.edit_message_text("Ye entry expire ho chuki hai, kripya /addpayment se dobara shuru karein.")
            return
        if not pending.get("amount") or not pending.get("date"):
            await query.edit_message_text(
                "⚠️ Amount ya date abhi bhi missing hai. Pehle /fixamount aur /fixdate se set karein, "
                "phir dobara Confirm button press karein."
            )
            return
        try:
            purchase_date = parse_flexible_date(pending["date"])
        except Exception:
            await query.edit_message_text("Date sahi format mein nahi hai. Kripya `/fixdate 2026-01-12` format use karein.")
            return

        receipt_id = db.add_purchase(
            user_id=pending["user_id"],
            name=pending["name"],
            username=pending["username"],
            course_name=pending["course"],
            amount=pending["amount"],
            purchase_date=purchase_date,
            screenshot_file_id=pending["screenshot_file_id"],
        )

        pdf_path = None
        try:
            pdf_path = generate_receipt_pdf(receipt_id[-8:].upper(), pending, purchase_date)
            with open(pdf_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f,
                    filename=f"Receipt_{pending['user_id']}.pdf",
                    caption="📄 Payment receipt attached."
                )
        except Exception as e:
            logger.error(f"Receipt generation/send failed: {e}")
        finally:
            if pdf_path and os.path.exists(pdf_path):
                os.remove(pdf_path)

        PENDING.pop(update.effective_user.id, None)
        await query.edit_message_text(
            f"✅ *Entry Successfully Saved*\n{DIVIDER}\nReceipt No.: `{receipt_id[-8:].upper()}`",
            parse_mode=ParseMode.MARKDOWN
        )


# ---------------------------------------------------------------------------
# /check <user_id> - full bio
# ---------------------------------------------------------------------------

@admin_only
async def check_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Sahi format: `/check 123456789`", parse_mode=ParseMode.MARKDOWN)
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

    lines = [f"👤 *Student Profile*", DIVIDER, f"*User ID:* `{user_id}`"]

    if purchases:
        purchase_name = purchases[0]["name_at_purchase"]
        purchase_username = purchases[0]["username_at_purchase"]
        lines.append(f"*Name (at purchase):* {purchase_name}")
        if current_name and current_name != purchase_name:
            lines.append(f"*Name (current):* {current_name} ⚠️ _(changed)_")
        elif current_name:
            lines.append(f"*Name (current):* {current_name} ✓ _(unchanged)_")
        lines.append(f"*Username (at purchase):* {purchase_username}")
        if current_username:
            lines.append(f"*Username (current):* {current_username}")
    elif current_name:
        lines.append(f"*Name:* {current_name}")
        lines.append(f"*Username:* {current_username}")

    lines.append("")
    lines.append(f"📚 *Purchase History*")
    lines.append(DIVIDER)
    if purchases:
        total = sum(float(p["amount"]) for p in purchases if p.get("amount"))
        for p in purchases:
            lines.append(f"• {p['course_name']} — ₹{p['amount']} — {p['purchase_date']}")
        lines.append(f"\n*Total Paid:* ₹{total:,.0f}")
    else:
        lines.append("_Koi purchase record uplabdh nahi hai._")

    lines.append("")
    lines.append(f"📢 *Channel Membership*")
    lines.append(DIVIDER)
    if channels:
        for c in channels:
            joined = c["joined_at"].strftime("%d %b %Y") if c["joined_at"] else "—"
            link = c["invite_link"] or "(link uplabdh nahi hai)"
            lines.append(f"• {c['title']} — joined {joined}\n  {link}")
    else:
        lines.append("_User is samay kisi bhi channel mein active nahi hai._")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


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
        [InlineKeyboardButton(f"📢 {c['title'] or c['channel_id']}", callback_data=f"chinfo_{c['channel_id']}")]
        for c in channels
    ]
    await update.message.reply_text(
        f"📢 *Registered Channels* ({len(channels)})",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
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
        await query.edit_message_text(f"📢 *{title}*\n\nIs channel mein abhi koi active member nahi hai.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"📢 *{title}*", DIVIDER, f"*Active Members:* {len(members)}", ""]
    for m in members:
        joined = m["joined_at"].strftime("%d %b %Y") if m.get("joined_at") else "—"
        lines.append(f"• `{m['user_id']}` — joined {joined}")

    text = "\n".join(lines)
    # Telegram message limit safeguard
    if len(text) > 4000:
        text = text[:3900] + "\n\n_...list truncated._"

    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Track channel membership automatically (join/leave)
# ---------------------------------------------------------------------------

async def track_bot_added_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when the BOT's own status changes in a chat (e.g. added as admin).
    Registers the channel immediately, and logs the admin who added the bot
    as the first member — instead of waiting for someone else to join/leave."""
    result = update.my_chat_member
    if result is None:
        return

    chat = result.chat
    new_status = result.new_chat_member.status

    if new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
        try:
            db.register_channel(channel_id=chat.id, title=chat.title, invite_link=None)
            # log the person who added/promoted the bot as a member too
            adder = result.from_user
            if adder and not adder.is_bot:
                db.log_join(user_id=adder.id, channel_id=chat.id)
        except Exception as e:
            logger.error(f"Failed to register channel {chat.id} on bot add: {e}")
    elif new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        # NOTE: database.py has no remove_channel function, so the channel
        # entry stays in the DB even if the bot is removed — harmless, but
        # /channels will keep listing it. Add a db.remove_channel() if you
        # want it to disappear automatically.
        logger.info(f"Bot removed from chat {chat.id}; channel record left as-is (no remove_channel in db).")


@admin_only
async def register_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually (re)register the channel this command is sent in — use this
    once per channel where the bot was already made admin BEFORE this
    tracking code existed. Telegram's Bot API cannot list a channel's full
    member list (only admins), so only current admins get backfilled."""
    chat = update.effective_chat
    if chat.type not in ("channel", "supergroup", "group"):
        await update.message.reply_text("Ye command kisi channel/group ke andar bhejein.")
        return

    try:
        db.register_channel(channel_id=chat.id, title=chat.title, invite_link=None)
        admins = await context.bot.get_chat_administrators(chat.id)
        count = 0
        for member in admins:
            if not member.user.is_bot:
                db.log_join(user_id=member.user.id, channel_id=chat.id)
                count += 1
        await update.message.reply_text(
            f"✅ *{chat.title}* register ho gaya.\n"
            f"{count} admin(s) bhi member ki tarah log ho gaye.\n\n"
            f"_Note: normal members (non-admin) automatically tab register honge "
            f"jab wo ab join/leave karenge — Telegram unki purani list nahi deta._",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Failed to register current chat {chat.id}: {e}")
        await update.message.reply_text("⚠️ Register karte waqt error aayi, logs check karein.")


async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if result is None:
        return

    chat = result.chat
    user = result.new_chat_member.user
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    was_member = old_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    is_member = new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

    try:
        if not was_member and is_member:
            # user joined
            db.log_join(user_id=user.id, channel_id=chat.id)
        elif was_member and not is_member:
            # user left/kicked
            db.log_leave(user_id=user.id, channel_id=chat.id)
    except Exception as e:
        logger.error(f"Failed to track chat member update for chat {chat.id}, user {user.id}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    app = Application.builder().token(BOT_TOKEN).build()

    db.init_db()

    # /addpayment conversation
    addpayment_conv = ConversationHandler(
        entry_points=[CommandHandler("addpayment", addpayment_start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_username)],
            ASK_USERID: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_userid)],
            ASK_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_course)],
            ASK_SCREENSHOT: [MessageHandler(filters.PHOTO, ask_screenshot)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(addpayment_conv)
    app.add_handler(CommandHandler("cancel", cancel_conversation))
    app.add_handler(CommandHandler("fixamount", fix_amount))
    app.add_handler(CommandHandler("fixdate", fix_date))
    app.add_handler(CommandHandler("check", check_user))
    app.add_handler(CommandHandler("channels", list_channels))
    app.add_handler(CommandHandler("registerhere", register_here))

    app.add_handler(CommandHandler("addadmin", add_admin_cmd))
    app.add_handler(CommandHandler("removeadmin", remove_admin_cmd))
    app.add_handler(CommandHandler("listadmins", list_admins_cmd))

    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(confirm_payment|cancel_payment)$"))
    app.add_handler(CallbackQueryHandler(channel_info_button, pattern="^chinfo_"))

    app.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(track_bot_added_to_channel, ChatMemberHandler.MY_CHAT_MEMBER))

    logger.info("Bot starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
