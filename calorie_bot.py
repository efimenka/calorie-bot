"""
Telegram Calorie Counter Bot
Использует Claude Vision API для распознавания еды и подсчёта калорий.

Установка:
    pip install python-telegram-bot anthropic

Запуск:
    TELEGRAM_TOKEN=... ANTHROPIC_API_KEY=... python calorie_bot.py
"""

import os
import base64
import logging
from io import BytesIO

import anthropic
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ── Настройки ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "ВАШ_TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "ВАШ_ANTHROPIC_KEY")
MODEL = "claude-sonnet-4-20250514"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Ты — опытный диетолог и нутрициолог.
Пользователь присылает фото блюда или продукта. Твоя задача:
1. Определить, что изображено на фото.
2. Оценить размер порции (визуально).
3. Рассчитать примерное количество калорий (ккал).
4. Указать КБЖУ: калории, белки, жиры, углеводы.
5. Дать короткий совет по питанию (1–2 предложения).

Отвечай по-русски, структурированно. Если на фото нет еды — скажи об этом.
Если изображение нечёткое — дай диапазон оценки.

Формат ответа:
🍽 **Блюдо:** [название]
📏 **Порция:** [примерный вес/объём]
🔥 **Калории:** [ккал]
💪 **Белки:** [г] | 🧈 **Жиры:** [г] | 🌾 **Углеводы:** [г]
💡 **Совет:** [совет]"""


# ── Хэндлеры ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветственное сообщение."""
    await update.message.reply_text(
        "👋 Привет! Я считаю калории по фото.\n\n"
        "📸 Просто отправь мне фото блюда или продукта, "
        "и я определю калорийность и КБЖУ.\n\n"
        "Команды:\n"
        "/start — это сообщение\n"
        "/help  — подробная справка"
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
        "• Если порция нестандартная — напиши её вес в подписи к фото\n\n"
        "⚠️ Оценка калорий приблизительная — ±20%.",
        parse_mode="Markdown",
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка входящего фото."""
    msg = update.message
    thinking = await msg.reply_text("🔍 Анализирую фото, подожди секунду…")

    try:
        # Берём фото наилучшего качества
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)

        buf = BytesIO()
        await file.download_to_memory(buf)
        image_b64 = base64.standard_b64encode(buf.getvalue()).decode()

        # Текстовая подсказка от пользователя (если есть подпись к фото)
        user_hint = msg.caption or "Определи блюдо и посчитай калории."

        response = claude.messages.create(
            model=MODEL,
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": user_hint},
                    ],
                }
            ],
        )

        answer = response.content[0].text
        await thinking.delete()
        await msg.reply_text(answer, parse_mode="Markdown")

    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        await thinking.edit_text("❌ Ошибка API. Попробуй ещё раз чуть позже.")
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        await thinking.edit_text("❌ Что-то пошло не так. Попробуй снова.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подсказка, если пользователь пишет текст вместо фото."""
    await update.message.reply_text(
        "📸 Отправь мне *фото* блюда, и я посчитаю калории!\n"
        "Текстовые запросы пока не поддерживаются.",
        parse_mode="Markdown",
    )


# ── Запуск ───────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен. Ожидаю сообщения…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
