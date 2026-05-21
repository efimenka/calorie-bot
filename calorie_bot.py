"""
Telegram Calorie Counter Bot - Yandex AI Studio (Qwen3.6-35B VLM)
"""

import os
import base64
import logging
from io import BytesIO

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "")
MODEL = "qwen3.6-35b-a3b/latest"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

PROMPT = """Ты опытный диетолог. Посмотри на фото еды и ответь строго в таком формате:
🍽 Блюдо: [название]
📏 Порция: [вес]
🔥 Калории: [ккал]
💪 Белки: [г] | 🧈 Жиры: [г] | 🌾 Углеводы: [г]
💡 Совет: [короткий совет]
Если на фото не еда — скажи об этом. Отвечай только по-русски."""


async def ask_yandex(image_b64: str) -> str:
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
        "x-folder-id": YANDEX_FOLDER_ID,
    }
    payload = {
        "model": f"gpt://{YANDEX_FOLDER_ID}/{MODEL}",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        "max_tokens": 800,
        "temperature": 0.3,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://ai.api.cloud.yandex.net/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        logger.info("Yandex response: %s %s", r.status_code, r.text[:500])
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я считаю калории по фото.\n📸 Отправь фото блюда — получи КБЖУ!\n/help — справка"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📸 Отправь фото еды → получи калории и КБЖУ.\n"
        "Можешь написать вес в подписи к фото.\n"
        "⚠️ Оценка приблизительная ±20%."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    thinking = await msg.reply_text("🔍 Анализирую фото…")
    try:
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        image_b64 = base64.standard_b64encode(buf.getvalue()).decode()
        answer = await ask_yandex(image_b64)
        await thinking.delete()
        await msg.reply_text(answer)
    except Exception as e:
        logger.error("Error: %s", e)
        await thinking.edit_text(f"❌ Ошибка: {str(e)[:200]}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📸 Отправь фото блюда!")

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
