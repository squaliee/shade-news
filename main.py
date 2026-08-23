import asyncio
import logging
import os
import sys
import re # Додано для очищення тексту від тегів

from aiogram import Bot, Dispatcher, html, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from supabase import Client, create_client

# --------------------------------------------------
# 1. Конфігурація та змінні середовища
# --------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8415128660:AAF0pcIL3w5Qkj8MsYLKqZpfDy3UvHKCh94")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://shade-news-app.vercel.app/")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://mdkupzrojmulqernsspo.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1ka3VwenJvam11bHFlcm5zc3BvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzE2ODY2MjksImV4cCI6MjA4NzI2MjYyOX0.N0NWe4ANf2hm-zUFhlCM6dV_8daTIOJWRnx-5e4uxdc")

# Назва твого каналу без '@'
TARGET_CHANNEL_USERNAME = "sh4denews"

# Підключення до Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    logging.warning(f"Supabase connection warning: {e}")
    supabase = None

# Ініціалізація бота та диспетчера
dp = Dispatcher()


# --------------------------------------------------
# 2. Обробники команд
# --------------------------------------------------
@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    """Обробник команди /start з кнопкою для запуску Mini App."""
    user = message.from_user
    username = html.quote(user.first_name) if user else "геймере"

    # Клавіатура для відкриття Mini App
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎮 Відкрити Shade News",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )

    welcome_text = (
        f"Привіт, {username}! 👾\n\n"
        "Ласкаво просимо до **Shade News** — твого головного хабу ігрових новин, "
        "релізів та знижок.\n\n"
        "Натискай кнопку нижче, щоб запустити додаток:"
    )

    await message.answer(welcome_text, reply_markup=keyboard)


# --------------------------------------------------
# 3. Обробник постів з каналу (НОВЕ)
# --------------------------------------------------
@dp.channel_post(F.chat.username == TARGET_CHANNEL_USERNAME)
async def capture_channel_post(message: Message) -> None:
    """Ловить нові публікації в каналі та зберігає в Supabase."""
    
    # Якщо немає підключення до БД, нічого не робимо
    if supabase is None:
        logging.error("Supabase is not connected. Cannot save post.")
        return

    try:
        # 1. Беремо текст посту (або caption, якщо це картинка з текстом)
        raw_text = message.html_text or message.caption or "Медіа файл 📸"
        
        # Легке очищення від зайвих HTML тегів, якщо треба (залишаємо як є, якщо хочемо форматування)
        # Для безпеки просто залишимо текст як є, але приберемо потенційні проблеми з лапками
        safe_text = raw_text.strip()
        
        # 2. Картинка. Поки що ставимо заглушку. 
        # (Telegram не дає вічних лінків на фото без завантаження)
        img_url = "https://images.unsplash.com/photo-1542751371-adc38448a05e?ixlib=rb-1.2.1&auto=format&fit=crop&w=600&q=80"
        
        # 3. Готуємо дані
        data = {
            "message_id": message.message_id,
            "text": safe_text,
            "image_url": img_url
        }
        
        # 4. Записуємо в таблицю 'news'
        response = supabase.table("news").insert(data).execute()
        
        logging.info(f"✅ Пост {message.message_id} успішно збережено в Supabase!")
        
    except Exception as e:
        logging.error(f"❌ Помилка при збереженні посту {message.message_id}: {e}")


# --------------------------------------------------
# 4. Точка входу
# --------------------------------------------------
async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        stream=sys.stdout,
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    logging.info("Starting Shade News Bot...")
    try:
        # Пропускаємо накопичені повідомлення за час простою
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())