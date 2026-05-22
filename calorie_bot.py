"""
Telegram Health Bot - Yandex AI Studio (Qwen3.6-35B VLM)
- Калории и БЖУ по фото еды
- Предупреждения о глютене, лактозе и сахаре
- Учёт тренировок (4 в неделю)
- Учёт бьюти-процедур (4 в неделю)
- Уведомления о воде, витаминах и рекомендации ужина
"""

import os
import re
import base64
import logging
from io import BytesIO
from datetime import time, datetime
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
WORKOUTS_PER_WEEK = 4
BEAUTY_PER_WEEK = 4

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# user_id -> {calories, protein, fat, carbs, date, week, workouts, beauty}
user_data: dict = {}

PROMPT_CLASSIFY = """Посмотри на фото и определи что на нём изображено. Ответь ОДНИМ словом:
- ЕДА — если на фото еда, блюдо, продукты питания
- ТРЕНИРОВКА — если на фото спортзал, тренажёры, спортивные упражнения, фитнес
- БЬЮТИ — если на фото косметический аппарат, массажёр, бьюти-девайс, процедура по уходу за собой
- ДРУГОЕ — если ничего из перечисленного

Отвечай только одним из четырёх слов."""

PROMPT_FOOD = """Ты диетолог с экспертизой в пищевых непереносимостях. Посмотри на фото еды и ответь строго в таком формате (без лишнего текста):

🍽 Блюдо: [название]
📏 Порция: [вес]
🔥 Калории: [только число ккал]
💪 Белки: [только число г] | 🧈 Жиры: [только число г] | 🌾 Углеводы: [только число г]
⚠️ Глютен: [ДА/НЕТ/ВОЗМОЖНО]
⚠️ Лактоза: [ДА/НЕТ/ВОЗМОЖНО]
⚠️ Сахар: [ДА/НЕТ/ВОЗМОЖНО]
💡 Совет: [короткий совет]

Если на фото не еда — напиши только: НЕ ЕДА
Отвечай только по-русски."""


async def ask_yandex_vision(image_b64: str, prompt: str) -> str:
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
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": 800,
        "temperature": 0.1,
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


def get_now_msk() -> datetime:
    return datetime.now(MSK)

def get_today_msk() -> str:
    return get_now_msk().strftime("%Y-%m-%d")

def get_week_msk() -> str:
    now = get_now_msk()
    return f"{now.year}-W{now.isocalendar()[1]}"


def get_user_data(user_id: int) -> dict:
    today = get_today_msk()
    week = get_week_msk()
    if user_id not in user_data:
        user_data[user_id] = {}
    d = user_data[user_id]
    if d.get("date") != today:
        d["calories"] = 0
        d["protein"] = 0.0
        d["fat"] = 0.0
        d["carbs"] = 0.0
        d["date"] = today
    if d.get("week") != week:
        d["workouts"] = 0
        d["beauty"] = 0
        d["week"] = week
    return d


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


def parse_allergens(text: str) -> list:
    warnings = []
    checks = [("Глютен", "🌾 ГЛЮТЕН"), ("Лактоза", "🥛 ЛАКТОЗА"), ("Сахар", "🍬 САХАР")]
    for key, label in checks:
        m = re.search(rf"{key}:\s*(ДА|НЕТ|ВОЗМОЖНО)", text, re.IGNORECASE)
        if m:
            val = m.group(1).upper()
            if val == "ДА":
                warnings.append(f"🚫 {label}: СОДЕРЖИТСЯ")
            elif val == "ВОЗМОЖНО":
                warnings.append(f"⚠️ {label}: ВОЗМОЖНО СОДЕРЖИТСЯ")
    return warnings


def week_progress(count: int, total: int) -> str:
    filled = "🟢" * count + "⚪️" * (total - count)
    return filled


# ── Хэндлеры ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    context.bot_data.setdefault("subscribers", set()).add(user_id)
    await update.message.reply_text(
        "👋 Привет! Я твой персональный помощник.\n\n"
        "📸 Отправь фото — я сам пойму что на нём:\n"
        "🥗 Еда → калории, КБЖУ, аллергены\n"
        "🏋️ Спортзал → засчитаю тренировку\n"
        "💆 Бьюти-аппарат → засчитаю процедуру\n\n"
        "🔔 Уведомления:\n"
        "• 💧 Вода — каждые 2 часа (8:00–22:00)\n"
        "• 💊 Витамины — в 9:00\n"
        "• 🍽 Ужин — рекомендации в 17:00\n\n"
        "/stats — полная статистика\n"
        "/reset — сбросить счётчик дня\n"
        "/help — справка"
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    d = get_user_data(user_id)
    left = CALORIE_LIMIT - d["calories"]
    cal_status = f"✅ Осталось: {left} ккал" if left > 0 else f"⚠️ Превышение на {abs(left)} ккал!"
    w = d.get("workouts", 0)
    b = d.get("beauty", 0)
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"*Питание за сегодня:*\n"
        f"🔥 {d['calories']} / {CALORIE_LIMIT} ккал — {cal_status}\n"
        f"💪 Белки: {d['protein']:.1f} г\n"
        f"🧈 Жиры: {d['fat']:.1f} г\n"
        f"🌾 Углеводы: {d['carbs']:.1f} г\n\n"
        f"*За эту неделю:*\n"
        f"🏋️ Тренировки: {w}/{WORKOUTS_PER_WEEK} {week_progress(w, WORKOUTS_PER_WEEK)}\n"
        f"💆 Бьюти-процедуры: {b}/{BEAUTY_PER_WEEK} {week_progress(b, BEAUTY_PER_WEEK)}",
        parse_mode="Markdown"
    )

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    d = get_user_data(user_id)
    d["calories"] = 0
    d["protein"] = 0.0
    d["fat"] = 0.0
    d["carbs"] = 0.0
    await update.message.reply_text("✅ Счётчик калорий и БЖУ за сегодня сброшен!")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📸 *Что можно отправить:*\n\n"
        "🥗 Фото еды → КБЖУ + аллергены + остаток калорий\n"
        "   _(напиши вес в подписи для точности)_\n\n"
        "🏋️ Фото из спортзала → засчитается тренировка\n\n"
        "💆 Фото бьюти-аппарата → засчитается процедура\n\n"
        "/stats — полная статистика\n"
        "/reset — сбросить счётчик дня\n"
        "⚠️ Оценка калорий приблизительная ±20%",
        parse_mode="Markdown"
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

        # Шаг 1: классифицируем фото
        category = (await ask_yandex_vision(image_b64, PROMPT_CLASSIFY)).strip().upper()
        logger.info("Photo category for user %s: %s", user_id, category)

        # Шаг 2: обрабатываем по категории
        if "ТРЕНИРОВКА" in category:
            d = get_user_data(user_id)
            if d["workouts"] < WORKOUTS_PER_WEEK:
                d["workouts"] += 1
            w = d["workouts"]
            await thinking.delete()
            await msg.reply_text(
                f"🏋️ *Тренировка засчитана!*\n\n"
                f"На этой неделе: {w}/{WORKOUTS_PER_WEEK} тренировок\n"
                f"{week_progress(w, WORKOUTS_PER_WEEK)}\n\n"
                f"{'✅ Недельная цель выполнена! 💪' if w >= WORKOUTS_PER_WEEK else f'До цели осталось: {WORKOUTS_PER_WEEK - w}'}",
                parse_mode="Markdown"
            )

        elif "БЬЮТИ" in category:
            d = get_user_data(user_id)
            if d["beauty"] < BEAUTY_PER_WEEK:
                d["beauty"] += 1
            b = d["beauty"]
            await thinking.delete()
            await msg.reply_text(
                f"💆 *Бьюти-процедура засчитана!*\n\n"
                f"На этой неделе: {b}/{BEAUTY_PER_WEEK} процедур\n"
                f"{week_progress(b, BEAUTY_PER_WEEK)}\n\n"
                f"{'✅ Недельная цель выполнена! ✨' if b >= BEAUTY_PER_WEEK else f'До цели осталось: {BEAUTY_PER_WEEK - b}'}",
                parse_mode="Markdown"
            )

        elif "ЕДА" in category:
            user_hint = msg.caption or ""
            if not user_hint:
                await msg.reply_text(
                    "💡 *Совет:* напиши вес в подписи к фото для точности\n"
                    "_(например: «250 г»)_",
                    parse_mode="Markdown"
                )

            food_prompt = PROMPT_FOOD
            if user_hint:
                food_prompt = f"Пользователь указал: {user_hint}\n\n" + PROMPT_FOOD

            answer = await ask_yandex_vision(image_b64, food_prompt)

            if "НЕ ЕДА" in answer.upper():
                await thinking.delete()
                await msg.reply_text("🙅 Не удалось распознать еду.")
                return

            kcal, protein, fat, carbs = parse_nutrition(answer)
            allergen_warnings = parse_allergens(answer)
            clean_answer = re.sub(r"⚠️\s*(Глютен|Лактоза|Сахар):.*\n?", "", answer).strip()

            if kcal > 0:
                d = add_nutrition(user_id, kcal, protein, fat, carbs)
                left = CALORIE_LIMIT - d["calories"]
                cal_status = f"✅ Осталось: {left} ккал" if left > 0 else f"⚠️ Превышение на {abs(left)} ккал!"
                allergen_block = (
                    "\n\n🚨 *Аллергены:*\n" + "\n".join(allergen_warnings)
                    if allergen_warnings
                    else "\n\n✅ *Глютен, лактоза и сахар не обнаружены*"
                )
                w = d.get("workouts", 0)
                b = d.get("beauty", 0)
                await thinking.delete()
                await msg.reply_text(
                    f"{clean_answer}"
                    f"{allergen_block}\n\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📊 *Итого за день:*\n"
                    f"🔥 {d['calories']}/{CALORIE_LIMIT} ккал — {cal_status}\n"
                    f"💪 Белки: {d['protein']:.1f} г\n"
                    f"🧈 Жиры: {d['fat']:.1f} г\n"
                    f"🌾 Углеводы: {d['carbs']:.1f} г\n\n"
                    f"*За эту неделю:*\n"
                    f"🏋️ Тренировки: {w}/{WORKOUTS_PER_WEEK} {week_progress(w, WORKOUTS_PER_WEEK)}\n"
                    f"💆 Процедуры: {b}/{BEAUTY_PER_WEEK} {week_progress(b, BEAUTY_PER_WEEK)}",
                    parse_mode="Markdown"
                )
            else:
                await thinking.delete()
                await msg.reply_text(answer)

        else:
            await thinking.delete()
            await msg.reply_text(
                "🤔 Не смогла распознать фото.\n\n"
                "Отправь:\n"
                "🥗 Фото еды\n"
                "🏋️ Фото из спортзала\n"
                "💆 Фото бьюти-аппарата"
            )

    except Exception as e:
        logger.error("Error: %s", e)
        await thinking.edit_text(f"❌ Ошибка: {str(e)[:200]}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user_id = update.effective_user.id
    context.bot_data.setdefault("subscribers", set()).add(user_id)
    text = msg.text.strip()

    thinking = await msg.reply_text("🔍 Считаю калории…")
    try:
        prompt = f"""Ты диетолог. Пользователь написал что съел: "{text}"
Рассчитай калории и БЖУ. Ответь строго в таком формате (без лишнего текста):

🍽 Блюдо: [название]
📏 Порция: [вес]
🔥 Калории: [только число ккал]
💪 Белки: [только число г] | 🧈 Жиры: [только число г] | 🌾 Углеводы: [только число г]
⚠️ Глютен: [ДА/НЕТ/ВОЗМОЖНО]
⚠️ Лактоза: [ДА/НЕТ/ВОЗМОЖНО]
⚠️ Сахар: [ДА/НЕТ/ВОЗМОЖНО]
💡 Совет: [короткий совет]

Если это не еда — напиши только: НЕ ЕДА
Отвечай только по-русски."""

        answer = await ask_yandex_text(prompt)

        if "НЕ ЕДА" in answer.upper():
            await thinking.delete()
            await msg.reply_text("🤔 Не похоже на еду. Напиши что съела, например: «творог 5% 200г»")
            return

        kcal, protein, fat, carbs = parse_nutrition(answer)
        allergen_warnings = parse_allergens(answer)
        clean_answer = re.sub(r"⚠️\s*(Глютен|Лактоза|Сахар):.*\n?", "", answer).strip()

        if kcal > 0:
            d = add_nutrition(user_id, kcal, protein, fat, carbs)
            left = CALORIE_LIMIT - d["calories"]
            cal_status = f"✅ Осталось: {left} ккал" if left > 0 else f"⚠️ Превышение на {abs(left)} ккал!"
            allergen_block = (
                "\n\n🚨 *Аллергены:*\n" + "\n".join(allergen_warnings)
                if allergen_warnings
                else "\n\n✅ *Глютен, лактоза и сахар не обнаружены*"
            )
            w = d.get("workouts", 0)
            b = d.get("beauty", 0)
            await thinking.delete()
            await msg.reply_text(
                f"{clean_answer}"
                f"{allergen_block}\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📊 *Итого за день:*\n"
                f"🔥 {d['calories']}/{CALORIE_LIMIT} ккал — {cal_status}\n"
                f"💪 Белки: {d['protein']:.1f} г\n"
                f"🧈 Жиры: {d['fat']:.1f} г\n"
                f"🌾 Углеводы: {d['carbs']:.1f} г\n\n"
                f"*За эту неделю:*\n"
                f"🏋️ Тренировки: {w}/{WORKOUTS_PER_WEEK} {week_progress(w, WORKOUTS_PER_WEEK)}\n"
                f"💆 Процедуры: {b}/{BEAUTY_PER_WEEK} {week_progress(b, BEAUTY_PER_WEEK)}",
                parse_mode="Markdown"
            )
        else:
            await thinking.delete()
            await msg.reply_text(answer)

    except Exception as e:
        logger.error("Text food error: %s", e)
        await thinking.edit_text(f"❌ Ошибка: {str(e)[:200]}")


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
    for user_id in context.bot_data.get("subscribers", set()):
        try:
            d = get_user_data(user_id)
            left = CALORIE_LIMIT - d["calories"]
            w = d.get("workouts", 0)
            b = d.get("beauty", 0)

            if left <= 0:
                await context.bot.send_message(
                    user_id,
                    f"🍽 *Время ужина!*\n\n"
                    f"⚠️ Лимит превышен на {abs(left)} ккал.\n"
                    f"Рекомендую только лёгкий салат (без глютена, лактозы и сахара).\n\n"
                    f"🏋️ Тренировки: {w}/{WORKOUTS_PER_WEEK} {week_progress(w, WORKOUTS_PER_WEEK)}\n"
                    f"💆 Процедуры: {b}/{BEAUTY_PER_WEEK} {week_progress(b, BEAUTY_PER_WEEK)}",
                    parse_mode="Markdown"
                )
                continue

            prompt = f"""Ты диетолог. Пользователь за день съел {d['calories']} ккал из лимита {CALORIE_LIMIT} ккал.
На ужин осталось примерно {left} ккал.
Белки: {d['protein']:.0f} г, Жиры: {d['fat']:.0f} г, Углеводы: {d['carbs']:.0f} г.
ВАЖНО: блюда БЕЗ глютена, БЕЗ лактозы и БЕЗ добавленного сахара.
Предложи ровно 3 варианта ужина. Формат:
🍽 Вариант 1: [название] — ~[ккал] ккал
[краткое описание]

🍽 Вариант 2: [название] — ~[ккал] ккал
[краткое описание]

🍽 Вариант 3: [название] — ~[ккал] ккал
[краткое описание]
Только по-русски."""

            suggestions = await ask_yandex_text(prompt)
            await context.bot.send_message(
                user_id,
                f"🌆 *Время ужина!*\n\n"
                f"📊 Съедено: {d['calories']}/{CALORIE_LIMIT} ккал\n"
                f"✅ Осталось: {left} ккал\n"
                f"_(все варианты без глютена, лактозы и сахара)_\n\n"
                f"{suggestions}\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"*За эту неделю:*\n"
                f"🏋️ Тренировки: {w}/{WORKOUTS_PER_WEEK} {week_progress(w, WORKOUTS_PER_WEEK)}\n"
                f"💆 Процедуры: {b}/{BEAUTY_PER_WEEK} {week_progress(b, BEAUTY_PER_WEEK)}",
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
