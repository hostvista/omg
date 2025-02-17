from dotenv import load_dotenv
load_dotenv()  # This must come before any other imports

import os
import random
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler
)
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
import fireworks.client
from fireworks.client.image import ImageInference, Answer

# Configure API keys using environment variables
fireworks.client.api_key = os.getenv("FIREWORKS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Database setup
Base = declarative_base()
engine = create_engine('sqlite:///indieai.db')
Session = sessionmaker(bind=engine)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, unique=True)
    username = Column(String)
    credits = Column(Integer, default=3)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    last_activity = Column(DateTime, default=datetime.now)

class Coupon(Base):
    __tablename__ = 'coupons'
    code = Column(String, primary_key=True)
    uses_left = Column(Integer)
    credits = Column(Integer)
    valid_until = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)

class ImageLog(Base):
    __tablename__ = 'image_logs'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer)
    prompt = Column(String)
    created_at = Column(DateTime, default=datetime.now)

Base.metadata.create_all(engine)

# Bot configuration
IMAGE_SIZES = [
    "640x1536",
    "768x1344", 
    "832x1216",
    "896x1152",
    "1024x1024",
    "1152x896",
    "1216x832",
    "1344x768",
    "1536x640"
]

DAILY_CREDITS = 3
ADMIN_IDS = [5500026782]  # Replace with actual admin IDs
SAMPLER = "DPMPP_2M_KARRAS"
CFG_SCALE = 7
STEPS = 100

# Telegram Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    session = Session()
    
    db_user = session.query(User).filter_by(user_id=user.id).first()
    if not db_user:
        db_user = User(
            user_id=user.id,
            username=user.username,
            credits=DAILY_CREDITS
        )
        session.add(db_user)
        session.commit()
    
    welcome_message = f"""
🌟 *Welcome to Indie AI 2\.0* 🌟

🖼️ Create stunning images with AI\-powered generation
🎁 Start with {DAILY_CREDITS} free daily credits
💡 Each generation costs 1 credit

✨ *Features:*
\- No content restrictions
\- Multiple image sizes
\- Daily free credits
\- Coupon redemption

📢 Type /create to start making magic!
    """
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=welcome_message,
        parse_mode="MarkdownV2"
    )

async def create_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 Please send me your image description\n"
        "Example: 'A cyberpunk cityscape at night'"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    user = session.query(User).filter_by(user_id=update.effective_user.id).first()
    
    if user.credits < 1:
        await update.message.reply_text("❌ Insufficient credits! Wait for daily reset or use /coupon")
        return
    
    context.user_data['prompt'] = update.message.text
    
    keyboard = [
        [InlineKeyboardButton(size, callback_data=size) for size in IMAGE_SIZES[i:i+3]]
        for i in range(0, len(IMAGE_SIZES), 3)
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🖼️ Choose image size:",
        reply_markup=reply_markup
    )

async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, prompt: str, width: int, height: int):
    session = Session()
    
    inference_client = ImageInference(model="accounts/fireworks/models/playground-v2-5-1024px-aesthetic")
    
    try:
        answer: Answer = inference_client.text_to_image(
            prompt=prompt,
            height=height,
            width=width,
            steps=STEPS,
            sampler=SAMPLER,
            cfg_scale=CFG_SCALE,
            safety_check=False,
            seed=random.randint(0, 1000000),
            output_image_format="JPG"
        )
        
        if answer.image:
            user = session.query(User).filter_by(user_id=user_id).first()
            user.credits -= 1
            user.last_activity = datetime.now()
            
            log = ImageLog(
                user_id=user_id,
                prompt=prompt
            )
            session.add(log)
            session.commit()
            
            image_path = f"output_{user_id}.jpg"
            answer.image.save(image_path)
            return image_path
    except Exception as e:
        print(f"Error generating image: {e}")
        return None

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = Session()
    user = session.query(User).filter_by(user_id=user_id).first()
    
    if not user or user.credits < 1:
        await query.edit_message_text("❌ Insufficient credits!")
        return
    
    size = query.data
    width, height = map(int, size.split('x'))
    prompt = context.user_data.get('prompt')
    
    await query.edit_message_text("🎨 Generating your image...")
    
    image_path = await generate_image(update, context, user_id, prompt, width, height)
    
    if image_path:
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=open(image_path, 'rb'),
            caption=f"🖼️ {prompt}\nSize: {size}\nCredits left: {user.credits}"
        )
        os.remove(image_path)
    else:
        await query.edit_message_text("❌ Failed to generate image")

async def coupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔑 Enter coupon code:")

async def handle_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.upper()
    session = Session()
    coupon = session.query(Coupon).filter_by(code=code).first()
    user = session.query(User).filter_by(user_id=update.effective_user.id).first()
    
    if not coupon or coupon.valid_until < datetime.now() or coupon.uses_left < 1:
        await update.message.reply_text("❌ Invalid or expired coupon")
        return
    
    user.credits += coupon.credits
    coupon.uses_left -= 1
    session.commit()
    
    await update.message.reply_text(f"🎉 Coupon redeemed! +{coupon.credits} credits")

def admin_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("❌ Admin access required")
            return
        return await func(update, context)
    return wrapper

@admin_required
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 User Stats", callback_data="admin_stats"),
         InlineKeyboardButton("🎫 Create Coupon", callback_data="admin_coupon")],
        [InlineKeyboardButton("🚫 Block User", callback_data="admin_block"),
         InlineKeyboardButton("📈 Usage Stats", callback_data="admin_usage")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🔒 Admin Panel:", reply_markup=reply_markup)

async def cron_daily_credits(context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    users = session.query(User).all()
    for user in users:
        user.credits = DAILY_CREDITS
    session.commit()

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("create", create_image))
    application.add_handler(CommandHandler("coupon", coupon_command))
    application.add_handler(CommandHandler("admin", admin_panel))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.job_queue.run_daily(
        cron_daily_credits,
        time=datetime.time(hour=0),
        name="daily_credit_reset"
    )
    
    application.run_polling()

if __name__ == "__main__":
    main()
