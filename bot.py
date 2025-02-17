import os
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)
import fireworks.client
from fireworks.client.image import ImageInference, Answer

# Initialize constants
API_KEY = os.getenv("FIREWORKS_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_NAME = "indie_ai.db"
ADMIN_USER_IDS = [5500026782]  # Replace with your admin user IDs

# Initialize Fireworks client
fireworks.client.api_key = API_KEY
inference_client = ImageInference(model="accounts/fireworks/models/playground-v2-5-1024px-aesthetic")

# Predefined size options
SIZE_OPTIONS = [
    ("640 x 1536", (640, 1536)),
    ("768 x 1344", (768, 1344)),
    ("832 x 1216", (832, 1216)),
    ("896 x 1152", (896, 1152)),
    ("1024 x 1024", (1024, 1024)),
    ("1152 x 896", (1152, 896)),
    ("1216 x 832", (1216, 832)),
    ("1344 x 768", (1344, 768)),
    ("1536 x 640", (1536, 640)),
]

# Conversation states
TEXT, SIZE_SELECTION, COUPON_CLAIM = range(3)

# Initialize database
conn = sqlite3.connect(DB_NAME, check_same_thread=False)
cursor = conn.cursor()

# Create tables
cursor.execute(
    """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                credits INTEGER DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
)

cursor.execute(
    """CREATE TABLE IF NOT EXISTS coupons (
                code TEXT PRIMARY KEY,
                credits INTEGER,
                max_uses INTEGER,
                used_count INTEGER DEFAULT 0,
                valid_until DATETIME)"""
)

cursor.execute(
    """CREATE TABLE IF NOT EXISTS generated_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
)

cursor.execute(
    """CREATE TABLE IF NOT EXISTS cheaters (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                reported_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
)

conn.commit()


# Helper function to check if user is admin
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


# Start command with welcome message
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Add user to database if not exists
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user.id,))
    conn.commit()

    # Send welcome message with image
    welcome_text = f"""
    🎉 Welcome to *Indie AI 2\.0* 🎉

    ✨ *Features:*
    \- Generate unlimited creative images
    \- No restrictions on content
    \- Simple credit system \(1 credit = 1 image\)
    \- Daily coupons available

    🖼️ To start creating, use /generate
    🎁 Claim coupons with /claim_coupon
    """

    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=open("welcome_image.jpg", "rb"),  # Add a welcome image file
        caption=welcome_text,
        parse_mode="MarkdownV2",
    )


# Generate image conversation
async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 Please enter your text prompt:")
    return TEXT


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["prompt"] = update.message.text
    # Create size selection buttons
    keyboard = []
    row = []
    for i, (label, _) in enumerate(SIZE_OPTIONS):
        row.append(InlineKeyboardButton(label, callback_data=str(i)))
        if (i + 1) % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "🖼️ Choose your image size:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SIZE_SELECTION


async def handle_size_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    selected_index = int(query.data)
    size_label, (width, height) = SIZE_OPTIONS[selected_index]

    # Store dimensions in context
    context.user_data["width"] = width
    context.user_data["height"] = height

    await query.edit_message_text(f"Selected size: {size_label}\nGenerating image...")

    # Check credits
    user_id = query.from_user.id
    cursor.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    credits = cursor.fetchone()[0]

    if credits < 1:
        await context.bot.send_message(
            chat_id=query.message.chat_id, text="❌ Insufficient credits! Use /claim_coupon"
        )
        return ConversationHandler.END

    # Deduct credit
    cursor.execute("UPDATE users SET credits = credits - 1 WHERE user_id = ?", (user_id,))
    cursor.execute("INSERT INTO generated_images (user_id) VALUES (?)", (user_id,))
    conn.commit()

    try:
        # Generate image with fixed steps=100 and safety_check=False
        answer: Answer = inference_client.text_to_image(
            prompt=context.user_data["prompt"],
            height=height,
            width=width,
            steps=100,  # Fixed step count
            safety_check=False,  # Safety filter off
            cfg_scale=7,
            sampler="DPMPP_2M_KARRAS",
            output_image_format="JPG",
        )

        if answer.image:
            answer.image.save("generated_image.jpg")
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=open("generated_image.jpg", "rb"),
                caption=f"🎨 Your {size_label} image!",
            )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id, text="❌ Failed to generate image. Please try again."
            )
    except Exception as e:
        await context.bot.send_message(
            chat_id=query.message.chat_id, text=f"⚠️ Error generating image: {str(e)}"
        )

    return ConversationHandler.END


# Coupon system
async def claim_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎫 Please enter your coupon code:")
    return COUPON_CLAIM


async def process_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.upper()
    user_id = update.effective_user.id

    cursor.execute(
        """SELECT * FROM coupons 
                   WHERE code = ? AND 
                   (valid_until > datetime('now') OR valid_until IS NULL) AND
                   used_count < max_uses""",
        (code,),
    )
    coupon = cursor.fetchone()

    if coupon:
        cursor.execute("UPDATE coupons SET used_count = used_count + 1 WHERE code = ?", (code,))
        cursor.execute(
            "UPDATE users SET credits = credits + ? WHERE user_id = ?", (coupon[1], user_id)
        )
        conn.commit()
        await update.message.reply_text(f"🎉 Coupon applied! You've received {coupon[1]} credits!")
    else:
        await update.message.reply_text("❌ Invalid or expired coupon code")

    return ConversationHandler.END


# Admin panel
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Access denied!")
        return

    keyboard = [
        [InlineKeyboardButton("📊 User Stats", callback_data="stats")],
        [InlineKeyboardButton("🎫 Create Coupon", callback_data="create_coupon")],
        [InlineKeyboardButton("🚫 Cheaters List", callback_data="cheaters")],
    ]

    await update.message.reply_text(
        "🔑 Admin Panel:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.answer("Access denied!")
        return

    if query.data == "stats":
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM generated_images")
        total_images = cursor.fetchone()[0]

        text = f"""📊 System Stats:
Users: {total_users}
Generated Images: {total_images}"""
        await query.edit_message_text(text)

    elif query.data == "cheaters":
        cursor.execute("SELECT * FROM cheaters")
        cheaters = cursor.fetchall()
        text = "🚫 Cheaters List:\n"
        for cheater in cheaters:
            text += f"User {cheater[0]} - {cheater[1]}\n"
        await query.edit_message_text(text)

    elif query.data == "create_coupon":
        await query.edit_message_text("🎫 Enter coupon details in the format:\n`code:credits:max_uses:validity_days`")
        return 1


async def create_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        code, credits, max_uses, validity_days = update.message.text.split(":")
        credits = int(credits)
        max_uses = int(max_uses)
        validity_days = int(validity_days)
        valid_until = datetime.now() + timedelta(days=validity_days)

        cursor.execute(
            "INSERT INTO coupons (code, credits, max_uses, valid_until) VALUES (?, ?, ?, ?)",
            (code, credits, max_uses, valid_until),
        )
        conn.commit()
        await update.message.reply_text(f"🎉 Coupon created: {code} ({credits} credits)")
    except Exception as e:
        await update.message.reply_text(f"❌ Error creating coupon: {str(e)}")

    return ConversationHandler.END


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Generate image conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("generate", generate_image)],
        states={
            TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)],
            SIZE_SELECTION: [CallbackQueryHandler(handle_size_selection)],
        },
        fallbacks=[],
    )

    # Coupon claim conversation handler
    coupon_handler = ConversationHandler(
        entry_points=[CommandHandler("claim_coupon", claim_coupon)],
        states={COUPON_CLAIM: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_coupon)]},
        fallbacks=[],
    )

    # Admin coupon creation handler
    admin_coupon_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_admin_callback, pattern="create_coupon")],
        states={1: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_coupon)]},
        fallbacks=[],
    )

    # Add all handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(conv_handler)
    application.add_handler(coupon_handler)
    application.add_handler(admin_coupon_handler)
    application.add_handler(CallbackQueryHandler(handle_admin_callback))

    application.run_polling()


if __name__ == "__main__":
    main()
