"""
Telegram Calorie Counter Bot - Yandex AI Studio (Qwen3.6-35B VLM)
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

user_data: dict = {}

PROMPT_PHOTO = """Ты опытный диетолог. Посмотри на фото еды и ответь строго в таком формате (без лишнего текста):
🍽 Блюдо: [название]
📏 Порция: [вес]
🔥 Калории: [только число ккал]
💪 Белки: [только число г] | 🧈 Жиры: [только число г] | 🌾 Углеводы: [только число г]
💡 Совет: [короткий совет]
Если на фото не еда — напиши только: НЕ ЕДА
Отвечай только по-русски."""


async def ask_yandex_text(prompt: str) -> str:
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
        "x-folder-id": YANDEX_FOLDER_ID,
    }
    payload = {
        "model": f"gpt://{YANDEX_FOLDER_ID}/{MODEL}",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 600,
        "temperature": 0.7,
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


async def ask_yandex_vision(image_b64: str) -> str:
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
                    {"type": "text", "text": PROMPT_PHOTO},
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


def get_user_data(user_id: int) -> dict:
    today = get_today_msk()
    if user_id not in user_data or user_data[user_id].get("date") != today:
        user_data[user_id] = {"calories": 0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "date": today}
    return user_data[user_id]


def add_nutrition(user_id: int, kcal: int, protein: float, fat: float, carbs: float) -> dict:
    d = get_user_data(user_id)
    d["calories"] += kcal
    d["protein"] += protein
    d["fat"] += fat
    d["carbs"] += carbs
    return d


def extract_number(pattern: str, text: str) -> float:
    m = re.search(pattern, text)
    return float(m.group(1).replace(",", ".")) if m else 0.0


def parse_nutrition(text: str):
    kcal = int(extract_number(r"Калории[:\s~]+(\d+)", text))
    protein = extract_number(r"Белки[:\s~]+(\d+[\.,]?\d*)", text)
    fat = extract_number(r"Жиры[:\s~]+(\d+[\.,]?\d*)", text)
    carbs = extract_number(r"Углеводы[:\s~]+(\d+[\.,]?\d*)", text)
    return kcal, protein, fat, carbs


# ── Хэндлеры ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    context.bot_data.setdefault("subscribers", set()).add(user_id)
    await update.message.reply_text(
        "👋 Привет! Я считаю калории и БЖУ по фото.\n\n"
        "📸 Отправь фото блюда — получи КБЖУ и остаток из 1400 ккал.\n\n"
        "🔔 Уведомления:\n"
        "• 💧 Вода — каждые 2 часа (8:00–22:00 МСК)\n"
        "• 💊 Витамины — в 9:00 МСК\n"
        "• 🍽 Рекомендация ужина — в 17:00 МСК\n\n"
        "/stats — статистика за сегодня\n"
        "/reset — сбросить счётчик\n"
        "/help — справка"
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    d = get_user_data(user_id)
    left = CALORIE_LIMIT - d["calories"]
    status = f"✅ Осталось: {left} ккал" if left > 0 else f"⚠️ Превышение на {abs(left)} ккал!"
    await update.message.reply_text(
        f"📊 *Статистика за сегодня:*\n\n"
        f"🔥 Калории: {d['calories']} / {CALORIE_LIMIT} ккал\n"
        f"{status}\n\n"
        f"💪 Белки: {d['protein']:.1f} г\n"
        f"🧈 Жиры: {d['fat']:.1f} г\n"
        f"🌾 Углеводы: {d['carbs']:.1f} г",
        parse_mode="Markdown"
    )

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_data[user_id] = {"calories": 0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "date": get_today_msk()}
    await update.message.reply_text("✅ Счётчик калорий и БЖУ сброшен!")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📸 Отправь фото еды → получи калории и КБЖУ.\n"
        "Можешь написать вес в подписи к фото.\n\n"
        "/stats — статистика за сегодня\n"
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
        answer = await ask_yandex_vision(image_b64)
        await thinking.delete()

        if "НЕ ЕДА" in answer.upper():
            await msg.reply_text("🙅 На фото не еда — калории не добавлены.")
            return

        kcal, protein, fat, carbs = parse_nutrition(answer)
        if kcal > 0:
            d = add_nutrition(user_id, kcal, protein, fat, carbs)
            left = CALORIE_LIMIT - d["calories"]
            status = f"✅ Осталось: {left} ккал" if left > 0 else f"⚠️ Превышение на {abs(left)} ккал!"
            await msg.reply_text(
                f"{answer}\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📊 *Итого за день:*\n"
                f"🔥 {d['calories']}/{CALORIE_LIMIT} ккал — {status}\n"
                f"💪 Белки: {d['protein']:.1f} г\n"
                f"🧈 Жиры: {d['fat']:.1f} г\n"
                f"🌾 Углеводы: {d['carbs']:.1f} г",
                parse_mode="Markdown"
            )
        else:
            await msg.reply_text(answer)
    except Exception as e:
        logger.error("Error: %s", e)
        await thinking.edit_text(f"❌ Ошибка: {str(e)[:200]}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📸 Отправь фото блюда!")


# ── Уведомления ───────────────────────────────────────────────────────────────

async def notify_water(context: ContextTypes.DEFAULT_TYPE) -> None:
    for user_id in context.bot_data.get("subscribers", set()):
        try:
            await context.bot.send_message(user_id, "💧 Время выпить воды! Рекомендуется 200–250 мл.")
        except Exception as e:
            logger.warning("Water notify failed for %s: %s", user_id, e)

async def notify_vitamins_morning(context: ContextTypes.DEFAULT_TYPE) -> None:
    for user_id in context.bot_data.get("subscribers", set()):
        try:
            await context.bot.send_message(user_id, "💊 Доброе утро! Не забудь выпить витамины! 🌅")
        except Exception as e:
            logger.warning("Vitamin notify failed for %s: %s", user_id, e)

async def notify_dinner(context: ContextTypes.DEFAULT_TYPE) -> None:
    """В 17:00 МСК — рекомендации ужина на основе остатка калорий."""
    for user_id in context.bot_data.get("subscribers", set()):
        try:
            d = get_user_data(user_id)
            left = CALORIE_LIMIT - d["calories"]

            if left <= 0:
                await context.bot.send_message(
                    user_id,
                    f"🍽 *Время ужина!*\n\n"
                    f"⚠️ Ты уже превысила дневной лимит на {abs(left)} ккал.\n"
                    f"Рекомендую на ужин только лёгкий салат или стакан кефира (до 100 ккал).",
                    parse_mode="Markdown"
                )
                continue

            prompt = f"""Ты диетолог. Пользователь за день съел {d['calories']} ккал из лимита {CALORIE_LIMIT} ккал.
На ужин осталось примерно {left} ккал.
Белки за день: {d['protein']:.0f} г, Жиры: {d['fat']:.0f} г, Углеводы: {d['carbs']:.0f} г.

Предложи ровно 3 варианта ужина которые вписываются в остаток калорий.
Формат ответа (строго):
🍽 Вариант 1: [название блюда] — ~[ккал] ккал
[краткое описание 1 строка]

🍽 Вариант 2: [название блюда] — ~[ккал] ккал
[краткое описание 1 строка]

🍽 Вариант 3: [название блюда] — ~[ккал] ккал
[краткое описание 1 строка]

Отвечай только по-русски, без лишних слов."""

            suggestions = await ask_yandex_text(prompt)
            await context.bot.send_message(
                user_id,
                f"🌆 *Время ужина!*\n\n"
                f"📊 За день съедено: {d['calories']}/{CALORIE_LIMIT} ккал\n"
                f"✅ Осталось: {left} ккал\n\n"
                f"Вот 3 варианта ужина для тебя:\n\n{suggestions}",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning("Dinner notify failed for %s: %s", user_id, e)


# ── Запуск ───────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    jq: JobQueue = app.job_queue
    for hour in range(8, 23, 2):
        jq.run_daily(notify_water, time=time(hour, 0, tzinfo=MSK))
    jq.run_daily(notify_vitamins_morning, time=time(9, 0, tzinfo=MSK))
    jq.run_daily(notify_dinner, time=time(17, 0, tzinfo=MSK))

    logger.info("Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
