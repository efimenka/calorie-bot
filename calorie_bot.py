"""
Telegram Calorie Counter Bot
Использует OpenRouter API для распознавания еды и подсчёта калорий.
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
 
# ── Настройки ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
 
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
 
SYSTEM_PROMPT = """Ты — опытный диетолог и нутрициолог.
Пользователь присылает фото блюда или продукта. Твоя задача:
1. Определить, что изображено на фото.
2. Оценить размер порции (визуально).
3. Рассчитать примерное количество калорий (ккал).
4. Указать КБЖУ: калории, белки, жиры, углеводы.
5. Дать короткий совет по питанию (1–2 предложения).
 
Отвечай по-русски, структурированно. Если на фото нет еды — скажи об этом.
 
Формат ответа:
🍽 *Блюдо:* [название]
📏 *Порция:* [примерный вес/объём]
🔥 *Калории:* [ккал]
💪 *Белки:* [г] | 🧈 *Жиры:* [г] | 🌾 *Углеводы:* [г]
💡 *Совет:* [совет]"""
 
 
async def ask_openrouter(image_b64: str, user_hint: str) -> str:
    """Отправляет фото в OpenRouter и возвращает ответ."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "google/gemini-2.0-flash-exp:free",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}"
                        },
                    },
                    {"type": "text", "text": user_hint},
                ],
            },
        ],
        "max_tokens": 800,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]
 
 
# ── Хэндлеры ─────────────────────────────────────────────────────────────────
 
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я считаю калории по фото.\n\n"
        "📸 Просто отправь мне фото блюда или продукта, "
        "и я определю калорийность и КБЖУ.\n\n"
        "/help — подробная справка"
    )
 
 
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ *Как пользоваться ботом:*\n\n"
        "1. Сфотографируй еду или продукт.\n"
        "2. Отправь фото прямо в этот чат.\n"
        "3. Получи оценку калорий и КБЖУ.\n\n"
        "*Советы для точного результата:*\n"
        "• Снимай при хорошем освещении\n"
        "• Старайся, чтобы блюдо было в кадре целиком\n"
        "• Можешь написать вес в подписи к фото\n\n"
        "⚠️ Оценка калорий приблизительная — ±20%.",
        parse_mode="Markdown",
    )
 
 
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    thinking = await msg.reply_text("🔍 Анализирую фото, подожди секунду…")
 
    try:
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
 
        buf = BytesIO()
        await file.download_to_memory(buf)
        image_b64 = base64.standard_b64encode(buf.getvalue()).decode()
 
        user_hint = msg.caption or "Определи блюдо и посчитай калории."
        answer = await ask_openrouter(image_b64, user_hint)
 
        await thinking.delete()
        await msg.reply_text(answer, parse_mode="Markdown")
 
    except Exception as e:
        logger.error("Error: %s", e)
        await thinking.edit_text("❌ Что-то пошло не так. Попробуй снова.")
 
 
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📸 Отправь мне *фото* блюда, и я посчитаю калории!",
        parse_mode="Markdown",
    )
 
 
# ── Запуск ───────────────────────────────────────────────────────────────────
 
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
