import os, logging, tempfile, asyncio
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
from lamix_api import verify_client, allocate_numbers, download_numbers, request_numbers_for_range
from ranges import LETTERS, get_ranges_by_letter, get_range_id

logger = logging.getLogger(__name__)

USERNAME, LETTER, RANGE_PAGE, RANGE_SELECT, QUANTITY = range(5)
PAGE_SIZE = 8

# ── Global allocation lock (prevents race conditions) ─────────────────────────
_alloc_lock = asyncio.Lock()


# ── Helper: track all bot message IDs for later deletion ─────────────────────
def _track(context, msg):
    """Save a sent message's id so we can delete it later."""
    if "msg_ids" not in context.user_data:
        context.user_data["msg_ids"] = []
    context.user_data["msg_ids"].append((msg.chat_id, msg.message_id))
    return msg


async def _delete_history(context, keep_message_ids: list = None):
    """Delete all tracked bot messages except those in keep_message_ids."""
    keep = set(keep_message_ids or [])
    for chat_id, msg_id in context.user_data.get("msg_ids", []):
        if msg_id in keep:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except BadRequest:
            pass  # Already deleted or too old
    # Also try to delete user messages
    for chat_id, msg_id in context.user_data.get("user_msg_ids", []):
        if msg_id in keep:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except BadRequest:
            pass


def _track_user(context, update):
    """Track user's own message for deletion."""
    if "user_msg_ids" not in context.user_data:
        context.user_data["user_msg_ids"] = []
    context.user_data["user_msg_ids"].append(
        (update.message.chat_id, update.message.message_id)
    )


# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    _track_user(context, update)
    msg = await update.message.reply_text(
        "👋 *Welcome to Lamix SMS Bot!*\n\nPlease enter your username:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    _track(context, msg)
    return USERNAME


# ── Step 1: Verify username ───────────────────────────────────────────────────
async def get_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track_user(context, update)
    username = update.message.text.strip()
    msg = await update.message.reply_text("🔍 Checking username...")
    _track(context, msg)

    client = verify_client(username)
    if not client:
        await msg.edit_text("❌ Username not found. Please try again:")
        return USERNAME

    context.user_data["username"]  = client["username"]
    context.user_data["client_id"] = client["client_id"]
    await msg.edit_text(f"✅ Verified! Welcome *{client['username']}*", parse_mode="Markdown")

    msg2 = await _show_letters(update)
    _track(context, msg2)
    return LETTER


async def _show_letters(update):
    rows = [LETTERS[i:i+5] for i in range(0, len(LETTERS), 5)]
    return await update.message.reply_text(
        "🌍 Select first letter of country:",
        reply_markup=ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True),
    )


# ── Step 2: Letter select ─────────────────────────────────────────────────────
async def pick_letter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track_user(context, update)
    letter = update.message.text.strip().upper()

    if letter not in LETTERS:
        msg = await _show_letters(update)
        _track(context, msg)
        return LETTER

    ranges = get_ranges_by_letter(letter)
    if not ranges:
        msg = await update.message.reply_text(f"❌ No ranges for '{letter}'. Try another:")
        _track(context, msg)
        msg2 = await _show_letters(update)
        _track(context, msg2)
        return LETTER

    context.user_data["letter"]        = letter
    context.user_data["letter_ranges"] = ranges
    context.user_data["page"]          = 0

    msg = await _show_range_page(update, context)
    _track(context, msg)
    return RANGE_PAGE


async def _show_range_page(update, context):
    ranges = context.user_data["letter_ranges"]
    page   = context.user_data["page"]
    total  = len(ranges)
    start  = page * PAGE_SIZE
    end    = min(start + PAGE_SIZE, total)
    chunk  = ranges[start:end]
    letter = context.user_data["letter"]

    keyboard = [[r["name"]] for r in chunk]
    nav = []
    if page > 0:
        nav.append("⬅️ Prev")
    if end < total:
        nav.append("➡️ Next")
    nav.append("🔤 Change Letter")
    keyboard.append(nav)

    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    return await update.message.reply_text(
        f"📋 *{letter}* — Page {page+1}/{pages} ({total} ranges):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )


# ── Step 3: Range page navigation ────────────────────────────────────────────
async def handle_range_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track_user(context, update)
    text = update.message.text.strip()

    if text == "⬅️ Prev":
        context.user_data["page"] = max(0, context.user_data["page"] - 1)
        msg = await _show_range_page(update, context)
        _track(context, msg)
        return RANGE_PAGE

    if text == "➡️ Next":
        ranges = context.user_data["letter_ranges"]
        max_page = (len(ranges) + PAGE_SIZE - 1) // PAGE_SIZE - 1
        context.user_data["page"] = min(max_page, context.user_data["page"] + 1)
        msg = await _show_range_page(update, context)
        _track(context, msg)
        return RANGE_PAGE

    if text == "🔤 Change Letter":
        msg = await _show_letters(update)
        _track(context, msg)
        return LETTER

    rid = get_range_id(text)
    if not rid:
        msg = await update.message.reply_text("❌ Please select from the list.")
        _track(context, msg)
        msg2 = await _show_range_page(update, context)
        _track(context, msg2)
        return RANGE_PAGE

    context.user_data["range_id"]   = rid
    context.user_data["range_name"] = text

    msg = await update.message.reply_text(
        f"✅ Selected: *{text}*\n\nNow send quantity (max 30):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    _track(context, msg)
    return QUANTITY


# ── Step 4: Quantity → Allocate ───────────────────────────────────────────────
async def get_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track_user(context, update)
    text = update.message.text.strip()

    if not text.isdigit() or int(text) <= 0:
        msg = await update.message.reply_text("❌ Please send a valid number (e.g. 20):")
        _track(context, msg)
        return QUANTITY

    if int(text) > 30:
        msg = await update.message.reply_text("❌ Maximum 30 numbers allowed per request. Please send a number ≤ 30:")
        _track(context, msg)
        return QUANTITY

    qty        = int(text)
    username   = context.user_data["username"]
    client_id  = context.user_data["client_id"]
    range_id   = context.user_data["range_id"]
    range_name = context.user_data["range_name"]

    # ── Queue message ──────────────────────────────────────────────────────────
    # If another allocation is in progress, wait for it to finish
    if _alloc_lock.locked():
        wait_msg = await update.message.reply_text(
            "⏳ Another request is in progress. Please wait..."
        )
        _track(context, wait_msg)

    async with _alloc_lock:
        queue_msg = await update.message.reply_text("✅ Added to queue\nPosition: 1")
        _track(context, queue_msg)

        result = allocate_numbers(client_id, range_id, qty)

        if not result["success"]:
            wait_msg2 = await update.message.reply_text(
                "⏳ This range has no numbers in your account right now.\n"
                "Checking if more numbers can be pulled from the pool...\n"
                "Please wait about 1 minute."
            )
            _track(context, wait_msg2)

            requested = request_numbers_for_range(range_id, qty=max(100, qty))

            if not requested:
                fail_msg = await update.message.reply_text(
                    "❌ The provider currently has no numbers available for this range.\n"
                    "Please try a different range. Use /start to begin."
                )
                _track(context, fail_msg)
                await _delete_history(context, keep_message_ids=[fail_msg.message_id])
                context.user_data.clear()
                return ConversationHandler.END

            await asyncio.sleep(60)
            result = allocate_numbers(client_id, range_id, qty)

            if not result["success"]:
                fail_msg = await update.message.reply_text(
                    "❌ Still no numbers available in this range.\n"
                    "Please try a different range. Use /start to begin."
                )
                _track(context, fail_msg)
                await _delete_history(context, keep_message_ids=[fail_msg.message_id])
                context.user_data.clear()
                return ConversationHandler.END

        cid     = result["client_id"] or client_id
        numbers = download_numbers(result["ealid"]) if result["ealid"] else []
        provided = len(numbers)

        # ── Alert message ──────────────────────────────────────────────────────
        alert = (
            f"📢 *ALERT*\n"
            f"Client Id - {cid} | Well Done! Numbers Allocated.\n\n"
            f"👤 {username}\n"
            f"📦 Requested: {qty} | 📨 Provided: {provided}\n\n"
            f"📱 *Numbers:*\n"
        )

        keep_ids = []  # These messages will NOT be deleted

        if numbers:
            full = alert + "\n".join(numbers)
            if len(full) <= 4096:
                alert_msg = await update.message.reply_text(full, parse_mode="Markdown")
                keep_ids.append(alert_msg.message_id)
            else:
                alert_msg = await update.message.reply_text(alert, parse_mode="Markdown")
                keep_ids.append(alert_msg.message_id)
                chunk = ""
                for num in numbers:
                    if len(chunk) + len(num) + 1 > 4000:
                        c_msg = await update.message.reply_text(chunk.strip())
                        keep_ids.append(c_msg.message_id)
                        chunk = ""
                    chunk += num + "\n"
                if chunk:
                    c_msg = await update.message.reply_text(chunk.strip())
                    keep_ids.append(c_msg.message_id)
        else:
            alert_msg = await update.message.reply_text(
                alert + "_(Numbers will be available shortly)_", parse_mode="Markdown"
            )
            keep_ids.append(alert_msg.message_id)

        # ── .txt file ──────────────────────────────────────────────────────────
        if numbers:
            safe  = range_name.replace(" ", "_").replace("/", "-")
            fname = f"{username}_{safe}_numbers.txt"
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write("\n".join(numbers))
                tmp = f.name
            with open(tmp, "rb") as f:
                file_msg = await update.message.reply_document(
                    document=f, filename=fname,
                    caption=f"📄 {provided} numbers — {range_name}"
                )
            os.unlink(tmp)
            keep_ids.append(file_msg.message_id)

        # ── Delete all previous chat history except numbers + file ─────────────
        await _delete_history(context, keep_message_ids=keep_ids)

        context.user_data.clear()
        return ConversationHandler.END


# ── /cancel ───────────────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _track_user(context, update)
    await _delete_history(context)
    context.user_data.clear()
    msg = await update.message.reply_text(
        "❌ Cancelled. Use /start to begin again.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END
