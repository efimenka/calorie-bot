"""
Telegram Calorie Counter Bot - Yandex AI Studio (Qwen3.6-35B VLM)
- Подсчёт калорий за день (лимит 1400 ккал)
- Уведомления о воде каждые 2 часа (8:00–22:00 МСК)
- Уведомления о витаминах в 9:00 и 17:00 МСК
"""

import os
import re
import base64
import logging
from io import BytesIO
from datetime import time
import pytz

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    JobQueue,
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "")
MODEL = "qwen3.6-35b-a3b/latest"
CALORIE_LIMIT = 1400
MSK = pytz.timezone("Europe/Moscow")

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# user_id -> {"calories": int, "date": str}
user_data: dict = {}

PROMPT = """Ты опытный диетолог. Посмотри на фото еды и ответь строго в таком формате (без лишнего текста):
🍽 Блюдо: [название]
📏 Порция: [вес]
🔥 Калории: [только число ккал]
💪 Белки: [г] | 🧈 Жиры: [г] | 🌾 Углеводы: [г]
💡 Совет: [короткий совет]
Если на фото не еда — напиши только: НЕ ЕДА
Отвечай только по-русски."""


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
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        "max_tokens": 800,
        "temperature": 0.3,
        "reasoning_effort": "none",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://ai.api.cloud.yandex.net/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def get_today_msk() -> str:
    from datetime import datetime
    return datetime.now(MSK).strftime("%Y-%m-%d")


def get_user_calories(user_id: int) -> int:
    today = get_today_msk()
    data = user_data.get(user_id, {})
    if data.get("date") != today:
        user_data[user_id] = {"calories": 0, "date": today}
    return user_data[user_id]["calories"]


def add_calories(user_id: int, kcal: int) -> int:
    today = get_today_msk()
    if user_id not in user_data or user_data[user_id].get("date") != today:
        user_data[user_id] = {"calories": 0, "date": today}
    user_data[user_id]["calories"] += kcal
    return user_data[user_id]["calories"]


def extract_calories(text: str) -> int | None:
    match = re.search(r"Калории[:\s~]+(\d+)", text)
    if match:
        return int(match.group(1))
    return None


# ── Хэндлеры ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    context.bot_data.setdefault("subscribers", set()).add(user_id)
    await update.message.reply_text(
        "👋 Привет! Я считаю калории по фото.\n\n"
        "📸 Отправь фото блюда — получи КБЖУ и остаток из 1400 ккал.\n\n"
        "🔔 Уведомления:\n"
        "• 💧 Вода — каждые 2 часа (8:00–22:00 МСК)\n"
        "• 💊 Витамины — в 9:00 и 17:00 МСК\n\n"
        "/calories — калории за сегодня\n"
        "/reset — сбросить счётчик\n"
        "/help — справка"
    )

async def cmd_calories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    eaten = get_user_calories(user_id)
    left = CALORIE_LIMIT - eaten
    if left > 0:
        await update.message.reply_text(
            f"📊 Сегодня съедено: {eaten} ккал\n"
            f"✅ Осталось: {left} ккал из {CALORIE_LIMIT}"
        )
    else:
        await update.message.reply_text(
            f"📊 Сегодня съедено: {eaten} ккал\n"
            f"⚠️ Лимит {CALORIE_LIMIT} ккал превышен на {abs(left)} ккал!"
        )

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_data[user_id] = {"calories": 0, "date": get_today_msk()}
    await update.message.reply_text("✅ Счётчик калорий сброшен!")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📸 Отправь фото еды → получи калории и КБЖУ.\n"
        "Можешь написать вес в подписи к фото.\n\n"
        "/calories — калории за сегодня\n"
        "/reset — сбросить счётчик\n"
        "⚠️ Оценка приблизительная ±20%."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user_id = update.effective_user.id
    context.bot_data.setdefault("subscribers", set()).add(user_id)
    thinking = await msg.reply_text("🔍 Анализирую фото…")
    try:
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        image_b64 = base64.standard_b64encode(buf.getvalue()).decode()
        answer = await ask_yandex(image_b64)
        await thinking.delete()

        if "НЕ ЕДА" in answer.upper():
            await msg.reply_text("🙅 На фото не еда — калории не добавлены.")
            return

        kcal = extract_calories(answer)
        if kcal:
            total = add_calories(user_id, kcal)
            left = CALORIE_LIMIT - total
            status = f"✅ Осталось: {left} ккал" if left > 0 else f"⚠️ Превышение на {abs(left)} ккал!"
            await msg.reply_text(f"{answer}\n\n📊 За сегодня: {total}/{CALORIE_LIMIT} ккал\n{status}")
        else:
            await msg.reply_text(answer)
    except Exception as e:
        logger.error("Error: %s", e)
        await thinking.edit_text(f"❌ Ошибка: {str(e)[:200]}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📸 Отправь фото блюда!")


# ── Уведомления ───────────────────────────────────────────────────────────────

async def notify_water(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = context.bot_data.get("subscribers", set())
    for user_id in subscribers:
        try:
            await context.bot.send_message(user_id, "💧 Время выпить воды! Рекомендуется 200–250 мл.")
        except Exception as e:
            logger.warning("Water notify failed for %s: %s", user_id, e)

async def notify_vitamins_morning(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = context.bot_data.get("subscribers", set())
    for user_id in subscribers:
        try:
            await context.bot.send_message(user_id, "💊 Доброе утро! Не забудь выпить витамины! 🌅")
        except Exception as e:
            logger.warning("Vitamin notify failed for %s: %s", user_id, e)

async def notify_vitamins_evening(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = context.bot_data.get("subscribers", set())
    for user_id in subscribers:
        try:
            await context.bot.send_message(user_id, "💊 Не забудь выпить вечерние витамины! 🌆")
        except Exception as e:
            logger.warning("Vitamin notify failed for %s: %s", user_id, e)


# ── Запуск ───────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("calories", cmd_calories))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    jq: JobQueue = app.job_queue

    # Вода каждые 2 часа с 8:00 до 22:00 МСК
    for hour in range(8, 23, 2):
        jq.run_daily(notify_water, time=time(hour, 0, tzinfo=MSK))

    # Витамины утром в 9:00 МСК
    jq.run_daily(notify_vitamins_morning, time=time(9, 0, tzinfo=MSK))

    # Витамины вечером в 17:00 МСК
    jq.run_daily(notify_vitamins_evening, time=time(17, 0, tzinfo=MSK))

    logger.info("Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
