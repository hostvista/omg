import os
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberStatus
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import fireworks.client
from fireworks.client.image import ImageInference, Answer

# Load environment variables
load_dotenv()
TOKEN = os.getenv('TELEGRAM_TOKEN')
FIREWORKS_API_KEY = os.getenv('FIREWORKS_API_KEY')
ADMIN_USER_IDS = [int(id.strip()) for id in os.getenv('ADMIN_USER_IDS', '').split(',') if id.strip()]

# Channel configuration
CHANNEL_USERNAME = "indie_ai"
CHANNEL_LINK = "https://t.me/indie_ai"

# Initialize Fireworks client
fireworks.client.api_key = FIREWORKS_API_KEY
inference_client = ImageInference(model="accounts/fireworks/models/playground-v2-5-1024px-aesthetic")

# Supported dimensions
SUPPORTED_DIMENSIONS = [
    (640, 1536),
    (768, 1344),
    (832, 1216),
    (896, 1152),
    (1024, 1024),
    (1152, 896),
    (1216, 832),
    (1344, 768),
    (1536, 640)
]

# Check channel membership
async def is_channel_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        chat_member = await context.bot.get_chat_member(f"@{CHANNEL_USERNAME}", update.effective_user.id)
        return chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception:
        return False

# Channel membership decorator
def channel_membership_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id in ADMIN_USER_IDS:
            return await func(update, context)
            
        if not await is_channel_member(update, context):
            keyboard = [[InlineKeyboardButton("Join Channel", url=CHANNEL_LINK)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "⚠️ You must join our channel to use this bot!\n\n"
                "1. Click the button below to join\n"
                "2. After joining, try your command again",
                reply_markup=reply_markup
            )
            return
        return await func(update, context)
    return wrapper

# Database setup
def setup_database():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    
    # Create users table with is_blocked field and preferred dimensions
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            credits INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            preferred_width INTEGER DEFAULT 1024,
            preferred_height INTEGER DEFAULT 1024,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create coupons table
    c.execute('''
        CREATE TABLE IF NOT EXISTS coupons (
            code TEXT PRIMARY KEY,
            credits INTEGER,
            is_used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER,
            FOREIGN KEY(created_by) REFERENCES users(user_id)
        )
    ''')
    
    conn.commit()
    conn.close()

# Admin check decorator
def admin_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_USER_IDS:
            await update.message.reply_text("This command is only available to administrators.")
            return
        return await func(update, context)
    return wrapper

# User management
def get_or_create_user(user_id: int, username: str) -> dict:
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = c.fetchone()
    
    if not user:
        c.execute('''
            INSERT INTO users (user_id, username, credits, is_blocked, preferred_width, preferred_height)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, username, 5, 0, 1024, 1024))  # Give 5 free credits to new users
        conn.commit()
        user = (user_id, username, 5, 0, 1024, 1024, datetime.now())
    
    conn.close()
    return {
        'user_id': user[0],
        'username': user[1],
        'credits': user[2],
        'is_blocked': user[3],
        'preferred_width': user[4],
        'preferred_height': user[5],
        'created_at': user[6]
    }

def update_credits(user_id: int, credits_change: int):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    
    c.execute('''
        UPDATE users 
        SET credits = credits + ? 
        WHERE user_id = ?
    ''', (credits_change, user_id))
    
    conn.commit()
    conn.close()

def update_user_dimensions(user_id: int, width: int, height: int):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    
    c.execute('''
        UPDATE users 
        SET preferred_width = ?, preferred_height = ? 
        WHERE user_id = ?
    ''', (width, height, user_id))
    
    conn.commit()
    conn.close()

# Coupon management
def create_coupon(code: str, credits: int, created_by: int):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    
    try:
        c.execute('''
            INSERT INTO coupons (code, credits, created_by)
            VALUES (?, ?, ?)
        ''', (code, credits, created_by))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

@channel_membership_required
async def claim_coupon(code: str, user_id: int) -> tuple[bool, str]:
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    
    # Check if user is blocked
    c.execute('SELECT is_blocked FROM users WHERE user_id = ?', (user_id,))
    user = c.fetchone()
    if user and user[0] == 1:
        conn.close()
        return False, "Your account has been blocked. Please contact an administrator."
    
    c.execute('SELECT * FROM coupons WHERE code = ? AND is_used = 0', (code,))
    coupon = c.fetchone()
    
    if not coupon:
        conn.close()
        return False, "Invalid or already used coupon code!"
    
    # Mark coupon as used and add credits to user
    c.execute('UPDATE coupons SET is_used = 1 WHERE code = ?', (code,))
    update_credits(user_id, coupon[1])
    
    conn.commit()
    conn.close()
    return True, f"Successfully claimed {coupon[1]} credits!"

# Admin functions
def get_all_users():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute('SELECT * FROM users')
    users = c.fetchall()
    conn.close()
    return users

def get_all_coupons():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute('''
        SELECT c.*, u.username 
        FROM coupons c 
        LEFT JOIN users u ON c.created_by = u.user_id
    ''')
    coupons = c.fetchall()
    conn.close()
    return coupons

def toggle_user_block(user_id: int) -> bool:
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    
    c.execute('SELECT is_blocked FROM users WHERE user_id = ?', (user_id,))
    user = c.fetchone()
    
    if user:
        new_status = 1 if user[0] == 0 else 0
        c.execute('UPDATE users SET is_blocked = ? WHERE user_id = ?', (new_status, user_id))
        conn.commit()
        conn.close()
        return new_status == 1
    
    conn.close()
    return False

# Bot command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_or_create_user(update.effective_user.id, update.effective_user.username)
    
    if user['is_blocked']:
        await update.message.reply_text("Your account has been blocked. Please contact an administrator.")
        return
    
    # Check channel membership
    if not await is_channel_member(update, context) and update.effective_user.id not in ADMIN_USER_IDS:
        keyboard = [[InlineKeyboardButton("Join Channel", url=CHANNEL_LINK)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Welcome to the Text-to-Image Bot!\n\n"
            "⚠️ You must join our channel to use this bot!\n\n"
            "1. Click the button below to join\n"
            "2. After joining, send /start again",
            reply_markup=reply_markup
        )
        return
    
    welcome_message = (
        f"Welcome to the Text-to-Image Bot!\n\n"
        f"You have {user['credits']} credits.\n"
        f"Each image generation costs 1 credit.\n\n"
        f"Commands:\n"
        f"/generate <prompt> - Generate an image\n"
        f"/dimensions - Set image dimensions\n"
        f"/credits - Check your credits\n"
        f"/claim <code> - Claim a coupon code"
    )
    
    if update.effective_user.id in ADMIN_USER_IDS:
        admin_commands = (
            f"\n\nAdmin Commands:\n"
            f"/users - List all users\n"
            f"/coupons - List all coupons\n"
            f"/createcoupon <code> <credits> - Create a new coupon\n"
            f"/block <user_id> - Block/unblock a user"
        )
        welcome_message += admin_commands
    
    await update.message.reply_text(welcome_message)

@channel_membership_required
async def check_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_or_create_user(update.effective_user.id, update.effective_user.username)
    if user['is_blocked']:
        await update.message.reply_text("Your account has been blocked. Please contact an administrator.")
        return
    await update.message.reply_text(f"You have {user['credits']} credits remaining.")

@channel_membership_required
async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide a coupon code: /claim <code>")
        return
    
    code = context.args[0]
    success, message = await claim_coupon(code, update.effective_user.id)
    await update.message.reply_text(message)

@channel_membership_required
async def set_dimensions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_or_create_user(update.effective_user.id, update.effective_user.username)
    
    if user['is_blocked']:
        await update.message.reply_text("Your account has been blocked. Please contact an administrator.")
        return
    
    keyboard = []
    for width, height in SUPPORTED_DIMENSIONS:
        callback_data = f"dim_{width}_{height}"
        button_text = f"{width} x {height}"
        if width == user['preferred_width'] and height == user['preferred_height']:
            button_text += " ✓"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Select your preferred image dimensions:",
        reply_markup=reply_markup
    )

@channel_membership_required
async def dimension_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Extract dimensions from callback data
    _, width, height = query.data.split('_')
    width, height = int(width), int(height)
    
    # Update user's preferred dimensions
    update_user_dimensions(query.from_user.id, width, height)
    
    await query.edit_message_text(
        f"Image dimensions set to {width} x {height}.\n"
        f"Use /generate to create an image with these dimensions."
    )

@channel_membership_required
async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_or_create_user(update.effective_user.id, update.effective_user.username)
    
    if user['is_blocked']:
        await update.message.reply_text("Your account has been blocked. Please contact an administrator.")
        return
    
    if user['credits'] <= 0:
        await update.message.reply_text("You don't have enough credits! Use a coupon code to get more credits.")
        return
    
    if not context.args:
        await update.message.reply_text("Please provide a prompt: /generate <prompt>")
        return
    
    prompt = " ".join(context.args)
    await update.message.reply_text(
        f"Generating your image ({user['preferred_width']}x{user['preferred_height']})... Please wait."
    )
    
    try:
        answer: Answer = inference_client.text_to_image(
            prompt=prompt,
            height=user['preferred_height'],
            width=user['preferred_width'],
            steps=100,  # Set steps to 100
            seed=0,
            safety_check=False,  # Safety filter is always off
            output_image_format="JPG"
        )
        
        if answer.image is None:
            raise RuntimeError(f"No return image, {answer.finish_reason}")
        
        # Save image temporarily
        temp_path = f"temp_{update.effective_user.id}.jpg"
        answer.image.save(temp_path)
        
        # Send image
        with open(temp_path, 'rb') as photo:
            await update.message.reply_photo(photo)
        
        # Remove temporary file
        os.remove(temp_path)
        
        # Deduct credit
        update_credits(user['user_id'], -1)
        await update.message.reply_text("Image generated successfully! 1 credit has been deducted.")
        
    except Exception as e:
        await update.message.reply_text(f"Sorry, there was an error generating your image: {str(e)}")

# Admin command handlers
@admin_required
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = get_all_users()
    if not users:
        await update.message.reply_text("No users found.")
        return
    
    message = "Users list:\n\n"
    for user in users:
        status = "🚫 Blocked" if user[3] else "✅ Active"
        message += (
            f"ID: {user[0]}\n"
            f"Username: {user[1]}\n"
            f"Credits: {user[2]}\n"
            f"Status: {status}\n"
            f"Dimensions: {user[4]}x{user[5]}\n"
            f"Created: {user[6]}\n\n"
        )
    
    await update.message.reply_text(message)

@admin_required
async def list_coupons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coupons = get_all_coupons()
    if not coupons:
        await update.message.reply_text("No coupons found.")
        return
    
    message = "Coupons list:\n\n"
    for coupon in coupons:
        status = "Used" if coupon[2] else "Available"
        creator = coupon[5] if coupon[5] else "System"
        message += f"Code: {coupon[0]}\nCredits: {coupon[1]}\nStatus: {status}\nCreated: {coupon[3]}\nCreated by: {creator}\n\n"
    
    await update.message.reply_text(message)

@admin_required
async def create_coupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Please provide both code and credits: /createcoupon <code> <credits>")
        return
    
    try:
        code = context.args[0]
        credits = int(context.args[1])
        
        if credits <= 0:
            await update.message.reply_text("Credits must be a positive number.")
            return
        
        success = create_coupon(code, credits, update.effective_user.id)
        if success:
            await update.message.reply_text(f"Coupon {code} created successfully with {credits} credits!")
        else:
            await update.message.reply_text("Failed to create coupon. Code might already exist.")
    except ValueError:
        await update.message.reply_text("Credits must be a valid number.")

@admin_required
async def toggle_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide a user ID: /block <user_id>")
        return
    
    try:
        user_id = int(context.args[0])
        is_blocked = toggle_user_block(user_id)
        status = "blocked" if is_blocked else "unblocked"
        await update.message.reply_text(f"User {user_id} has been {status}.")
    except ValueError:
        await update.message.reply_text("Please provide a valid user ID.")

def main():
    # Setup database
    setup_database()
    
    # Initialize bot
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("credits", check_credits))
    application.add_handler(CommandHandler("claim", claim))
    application.add_handler(CommandHandler("generate", generate_image))
    application.add_handler(CommandHandler("dimensions", set_dimensions))
    
    # Admin handlers
    application.add_handler(CommandHandler("users", list_users))
    application.add_handler(CommandHandler("coupons", list_coupons))
    application.add_handler(CommandHandler("createcoupon", create_coupon_command))
    application.add_handler(CommandHandler("block", toggle_block))
    
    # Callback handler for dimension selection
    application.add_handler(CallbackQueryHandler(dimension_callback, pattern="^dim_"))
    
    # Start bot
    application.run_polling()

if __name__ == '__main__':
    main()