"""
Telegram Health Bot - Yandex AI Studio (Qwen3.6-35B VLM)
"""

import os
import re
import base64
import logging
from io import BytesIO
from datetime import time, datetime
import pytz

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

# Норма белка для женщины 37 лет ~60-75г в день (0.8-1.0г на кг, средний вес 65кг)
PROTEIN_MIN = 60.0
PROTEIN_RECOMMENDED = 75.0

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

user_data: dict = {}

PROMPT_CLASSIFY = """Посмотри на фото и определи что на нём. Ответь ОДНИМ словом:
- ЕДА — еда, блюдо, продукты питания
- ТРЕНИРОВКА — спортзал, тренажёры, упражнения, фитнес
- БЬЮТИ — косметический аппарат, массажёр, бьюти-девайс, процедура ухода
- ДРУГОЕ — ничего из перечисленного
Отвечай только одним из четырёх слов."""

PROMPT_FOOD = """Ты диетолог. Посмотри на фото еды и ответь строго в таком формате:

🍽 Блюдо: [название]
📏 Порция: [вес]
🔥 Калории: [только число ккал]
💪 Белки: [только число г] | 🧈 Жиры: [только число г] | 🌾 Углеводы: [только число г]
⚠️ Глютен: [ДА/НЕТ/ВОЗМОЖНО]
⚠️ Лактоза: [ДА/НЕТ/ВОЗМОЖНО]
⚠️ Сахар: [ДА/НЕТ/ВОЗМОЖНО]
💡 Совет: [короткий совет]

Если не еда — напиши только: НЕ ЕДА
Отвечай по-русски."""


async def ask_yandex_vision(image_b64: str, prompt: str) -> str:
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
        "x-folder-id": YANDEX_FOLDER_ID,
    }
    payload = {
        "model": f"gpt://{YANDEX_FOLDER_ID}/{MODEL}",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 800, "temperature": 0.1, "reasoning_effort": "none",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post("https://ai.api.cloud.yandex.net/v1/chat/completions", headers=headers, json=payload)
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
        "max_tokens": 600, "temperature": 0.7, "reasoning_effort": "none",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post("https://ai.api.cloud.yandex.net/v1/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def get_today_msk() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")

def get_week_msk() -> str:
    now = datetime.now(MSK)
    return f"{now.year}-W{now.isocalendar()[1]}"

def get_user_data(user_id: int) -> dict:
    today = get_today_msk()
    week = get_week_msk()
    if user_id not in user_data:
        user_data[user_id] = {}
    d = user_data[user_id]
    if d.get("date") != today:
        d.update({"calories": 0, "protein": 0.0, "fat": 0.0, "carbs": 0.0,
                   "date": today, "vitamins_taken": False})
    if d.get("week") != week:
        d.update({"workouts": 0, "beauty": 0, "week": week})
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
    for key, label in [("Глютен", "🌾 ГЛЮТЕН"), ("Лактоза", "🥛 ЛАКТОЗА"), ("Сахар", "🍬 САХАР")]:
        m = re.search(rf"{key}:\s*(ДА|НЕТ|ВОЗМОЖНО)", text, re.IGNORECASE)
        if m:
            val = m.group(1).upper()
            if val == "ДА":
                warnings.append(f"🚫 {label}: СОДЕРЖИТСЯ")
            elif val == "ВОЗМОЖНО":
                warnings.append(f"⚠️ {label}: ВОЗМОЖНО СОДЕРЖИТСЯ")
    return warnings

def week_progress(count: int, total: int) -> str:
    return "🟢" * count + "⚪️" * (total - count)

def protein_status(protein: float) -> str:
    if protein < PROTEIN_MIN:
        deficit = PROTEIN_MIN - protein
        return (f"⚠️ *Белок:* {protein:.1f} г — МАЛО!\n"
                f"Норма для тебя: {PROTEIN_MIN:.0f}–{PROTEIN_RECOMMENDED:.0f} г/день\n"
                f"Нужно добавить ещё ~{deficit:.0f} г белка\n"
                f"💡 Добавь: куриную грудку, яйца, рыбу или бобовые")
    elif protein < PROTEIN_RECOMMENDED:
        return f"✅ *Белок:* {protein:.1f} г — норма (рекомендуется до {PROTEIN_RECOMMENDED:.0f} г)"
    else:
        return f"💪 *Белок:* {protein:.1f} г — отлично!"

def build_stats_text(d: dict) -> str:
    left = CALORIE_LIMIT - d["calories"]
    cal_status = f"✅ Осталось: {left} ккал" if left > 0 else f"⚠️ Превышение на {abs(left)} ккал!"
    vitamins = "✅ Выпиты" if d.get("vitamins_taken") else "❌ Не отмечены"
    w = d.get("workouts", 0)
    b = d.get("beauty", 0)
    return (
        f"📊 *Статистика за сегодня:*\n\n"
        f"*Питание:*\n"
        f"🔥 {d['calories']}/{CALORIE_LIMIT} ккал — {cal_status}\n"
        f"{protein_status(d['protein'])}\n"
        f"🧈 Жиры: {d['fat']:.1f} г\n"
        f"🌾 Углеводы: {d['carbs']:.1f} г\n\n"
        f"💊 Витамины: {vitamins}\n\n"
        f"*За эту неделю:*\n"
        f"🏋️ Тренировки: {w}/{WORKOUTS_PER_WEEK} {week_progress(w, WORKOUTS_PER_WEEK)}\n"
        f"💆 Процедуры: {b}/{BEAUTY_PER_WEEK} {week_progress(b, BEAUTY_PER_WEEK)}"
    )

def build_food_response(answer: str, d: dict) -> str:
    left = CALORIE_LIMIT - d["calories"]
    cal_status = f"✅ Осталось: {left} ккал" if left > 0 else f"⚠️ Превышение на {abs(left)} ккал!"
    clean_answer = re.sub(r"⚠️\s*(Глютен|Лактоза|Сахар):.*\n?", "", answer).strip()
    allergen_warnings = parse_allergens(answer)
    allergen_block = (
        "\n\n🚨 *Аллергены:*\n" + "\n".join(allergen_warnings)
        if allergen_warnings else "\n\n✅ *Глютен, лактоза и сахар не обнаружены*"
    )
    w = d.get("workouts", 0)
    b = d.get("beauty", 0)
    return (
        f"{clean_answer}"
        f"{allergen_block}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 *Итого за день:*\n"
        f"🔥 {d['calories']}/{CALORIE_LIMIT} ккал — {cal_status}\n"
        f"{protein_status(d['protein'])}\n"
        f"🧈 Жиры: {d['fat']:.1f} г\n"
        f"🌾 Углеводы: {d['carbs']:.1f} г\n\n"
        f"*За эту неделю:*\n"
        f"🏋️ Тренировки: {w}/{WORKOUTS_PER_WEEK} {week_progress(w, WORKOUTS_PER_WEEK)}\n"
        f"💆 Процедуры: {b}/{BEAUTY_PER_WEEK} {week_progress(b, BEAUTY_PER_WEEK)}"
    )


# ── Хэндлеры ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    context.bot_data.setdefault("subscribers", set()).add(user_id)
    await update.message.reply_text(
        "👋 Привет! Я твой персональный помощник.\n\n"
        "📸 Отправь фото или напиши текстом:\n"
        "🥗 Еда → калории, КБЖУ, аллергены\n"
        "🏋️ Спортзал → засчитаю тренировку\n"
        "💆 Бьюти-аппарат → засчитаю процедуру\n\n"
        "🔔 Уведомления:\n"
        "• 💧 Вода — каждые 2 часа (8:00–22:00)\n"
        "• 💊 Витамины — каждые 3 часа с 10:00 (пока не отметишь)\n"
        "• 🍽 Ужин с рекомендациями — в 17:00\n\n"
        "/stats — полная статистика\n"
        "/vitamins — отметить витамины\n"
        "/reset — сбросить счётчик дня"
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = get_user_data(update.effective_user.id)
    await update.message.reply_text(build_stats_text(d), parse_mode="Markdown")

async def cmd_vitamins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    d = get_user_data(user_id)
    if d.get("vitamins_taken"):
        await update.message.reply_text("✅ Витамины уже отмечены на сегодня!")
    else:
        d["vitamins_taken"] = True
        await update.message.reply_text("✅ Витамины записаны! Молодец 💊")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    d = get_user_data(user_id)
    d.update({"calories": 0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "vitamins_taken": False})
    await update.message.reply_text("✅ Счётчик дня сброшен!")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📸 *Что можно отправить:*\n\n"
        "🥗 Фото или текст еды → КБЖУ + аллергены + белок\n"
        "   _(напиши вес в подписи к фото для точности)_\n\n"
        "🏋️ Фото из спортзала → тренировка\n"
        "💆 Фото бьюти-аппарата → процедура\n\n"
        "/stats — полная статистика\n"
        "/vitamins — отметить витамины\n"
        "/reset — сбросить счётчик дня",
        parse_mode="Markdown"
    )

async def handle_vitamin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if query.data == "vitamins_yes":
        d = get_user_data(user_id)
        d["vitamins_taken"] = True
        await query.edit_message_text("✅ Отлично! Витамины записаны на сегодня 💊")
    elif query.data == "vitamins_no":
        await query.edit_message_text("🔔 Хорошо, напомню позже!")

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

        category = (await ask_yandex_vision(image_b64, PROMPT_CLASSIFY)).strip().upper()
        logger.info("Category for user %s: %s", user_id, category)

        if "ТРЕНИРОВКА" in category:
            d = get_user_data(user_id)
            if d["workouts"] < WORKOUTS_PER_WEEK:
                d["workouts"] += 1
            w = d["workouts"]
            await thinking.delete()
            await msg.reply_text(
                f"🏋️ *Тренировка засчитана!*\n\n"
                f"На этой неделе: {w}/{WORKOUTS_PER_WEEK}\n"
                f"{week_progress(w, WORKOUTS_PER_WEEK)}\n\n"
                f"{'✅ Цель недели выполнена! 💪' if w >= WORKOUTS_PER_WEEK else f'Осталось: {WORKOUTS_PER_WEEK - w}'}",
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
                f"На этой неделе: {b}/{BEAUTY_PER_WEEK}\n"
                f"{week_progress(b, BEAUTY_PER_WEEK)}\n\n"
                f"{'✅ Цель недели выполнена! ✨' if b >= BEAUTY_PER_WEEK else f'Осталось: {BEAUTY_PER_WEEK - b}'}",
                parse_mode="Markdown"
            )

        elif "ЕДА" in category:
            user_hint = msg.caption or ""
            if not user_hint:
                await msg.reply_text(
                    "💡 *Совет:* напиши вес в подписи к фото для точности _(например: «250 г»)_",
                    parse_mode="Markdown"
                )
            food_prompt = (f"Пользователь указал: {user_hint}\n\n" + PROMPT_FOOD) if user_hint else PROMPT_FOOD
            answer = await ask_yandex_vision(image_b64, food_prompt)
            if "НЕ ЕДА" in answer.upper():
                await thinking.delete()
                await msg.reply_text("🙅 Не удалось распознать еду.")
                return
            kcal, protein, fat, carbs = parse_nutrition(answer)
            if kcal > 0:
                d = add_nutrition(user_id, kcal, protein, fat, carbs)
                await thinking.delete()
                await msg.reply_text(build_food_response(answer, d), parse_mode="Markdown")
            else:
                await thinking.delete()
                await msg.reply_text(answer)
        else:
            await thinking.delete()
            await msg.reply_text(
                "🤔 Не смогла распознать фото.\n\n"
                "Отправь: 🥗 еду | 🏋️ спортзал | 💆 бьюти-аппарат"
            )
    except Exception as e:
        logger.error("Error: %s", e)
        await thinking.edit_text(f"❌ Ошибка: {str(e)[:200]}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user_id = update.effective_user.id
    context.bot_data.setdefault("subscribers", set()).add(user_id)
    text = msg.text.strip()

    # Режим холодильника — если пользователь в режиме ввода продуктов
    fridge_mode = context.bot_data.get("fridge_mode", set())
    if user_id in fridge_mode:
        fridge_mode.discard(user_id)
        thinking = await msg.reply_text("🧑‍🍳 Составляю меню на завтра…")
        try:
            d = get_user_data(user_id)
            menu = await make_menu_from_fridge(text, current_protein=d.get("protein", 0.0))
            await thinking.delete()
            await msg.reply_text(
                f"🧊 *Твои продукты:* {text}\n\n{menu}",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error("Fridge menu error: %s", e)
            await thinking.edit_text(f"❌ Ошибка: {str(e)[:200]}")
        return

    # Команда для ручного вызова режима холодильника
    if any(w in text.lower() for w in ["холодильник", "меню на завтра", "что приготовить", "рецепты"]):
        context.bot_data.setdefault("fridge_mode", set()).add(user_id)
        await msg.reply_text(
            "🧊 Напиши что есть в холодильнике — составлю меню на день!\n\n"
            "Пример: _курица, яйца, огурцы, помидоры, рис_",
            parse_mode="Markdown"
        )
        return

    thinking = await msg.reply_text("🔍 Считаю калории…")
    try:
        prompt = f"""Ты диетолог. Пользователь написал что съел: "{text}"
Рассчитай КБЖУ. Ответь строго в формате:

🍽 Блюдо: [название]
📏 Порция: [вес]
🔥 Калории: [только число ккал]
💪 Белки: [только число г] | 🧈 Жиры: [только число г] | 🌾 Углеводы: [только число г]
⚠️ Глютен: [ДА/НЕТ/ВОЗМОЖНО]
⚠️ Лактоза: [ДА/НЕТ/ВОЗМОЖНО]
⚠️ Сахар: [ДА/НЕТ/ВОЗМОЖНО]
💡 Совет: [короткий совет]

Если не еда — напиши только: НЕ ЕДА
Отвечай по-русски."""

        answer = await ask_yandex_text(prompt)
        if "НЕ ЕДА" in answer.upper():
            await thinking.delete()
            await msg.reply_text("🤔 Не похоже на еду. Напиши что съела, например: «творог 5% 200г»")
            return
        kcal, protein, fat, carbs = parse_nutrition(answer)
        if kcal > 0:
            d = add_nutrition(user_id, kcal, protein, fat, carbs)
            await thinking.delete()
            await msg.reply_text(build_food_response(answer, d), parse_mode="Markdown")
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

async def notify_vitamins_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Спрашивает о витаминах каждые 3 часа начиная с 10:00 — пока не отмечены."""
    for user_id in context.bot_data.get("subscribers", set()):
        try:
            d = get_user_data(user_id)
            if d.get("vitamins_taken"):
                continue  # Уже выпиты — не спрашиваем
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Да, выпила!", callback_data="vitamins_yes"),
                    InlineKeyboardButton("⏰ Позже", callback_data="vitamins_no"),
                ]
            ])
            await context.bot.send_message(
                user_id,
                "💊 Ты уже выпила витамины сегодня?",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.warning("Vitamin check failed for %s: %s", user_id, e)

async def cmd_fridge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    context.bot_data.setdefault("fridge_mode", set()).add(user_id)
    await update.message.reply_text(
        "🧊 Напиши что есть в холодильнике — составлю меню на день!\n\n"
        "Пример: _курица, яйца, огурцы, помидоры, рис, оливковое масло_",
        parse_mode="Markdown"
    )


    """В 20:00 спрашивает что есть в холодильнике."""
    for user_id in context.bot_data.get("subscribers", set()):
        try:
            context.bot_data.setdefault("fridge_mode", set()).add(user_id)
            await context.bot.send_message(
                user_id,
                "🧊 *Что есть в холодильнике?*\n\n"
                "Напиши список продуктов, и я составлю меню на завтра на 1400 ккал "
                "без глютена, лактозы и сахара — и скажу что докупить!\n\n"
                "Пример: _курица, яйца, огурцы, помидоры, рис, оливковое масло_",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning("Fridge notify failed for %s: %s", user_id, e)


async def make_menu_from_fridge(products: str, current_protein: float = 0.0) -> str:
    protein_note = (
        f"Уже съедено белка: {current_protein:.0f} г. Нужно добрать ещё ~{max(0, PROTEIN_MIN - current_protein):.0f} г."
        if current_protein > 0
        else f"Норма белка на день: {PROTEIN_MIN:.0f}–{PROTEIN_RECOMMENDED:.0f} г."
    )
    prompt = f"""Ты диетолог и повар. Составь меню на один день (1400 ккал) для женщины 37 лет.

Доступные продукты: {products}

Требования:
- Без глютена, без лактозы, без добавленного сахара
- 3 приёма пищи: завтрак, обед, ужин (без перекуса)
- Итого ~1400 ккал
- {protein_note} Распредели белок равномерно по приёмам пищи.
- Для каждого приёма пищи указывай калории И белок

Формат ответа:

🌅 *Завтрак* (~[ккал] ккал | белок ~[г] г)
[блюдо и краткий рецепт 2-3 строки]

☀️ *Обед* (~[ккал] ккал | белок ~[г] г)
[блюдо и краткий рецепт 2-3 строки]

🌙 *Ужин* (~[ккал] ккал | белок ~[г] г)
[блюдо и краткий рецепт 2-3 строки]

📊 *Итого:* ~1400 ккал | Белки: ~[г] г | Жиры: ~[г] г | Углеводы: ~[г] г

🛒 *Что докупить:*
[список продуктов которых не хватает, или "Всё есть!"]

Отвечай только по-русски."""
    return await ask_yandex_text(prompt)


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
                    f"🍽 *Время ужина!*\n\n⚠️ Лимит превышен на {abs(left)} ккал.\n"
                    f"Рекомендую только лёгкий салат (без глютена, лактозы и сахара).\n\n"
                    f"🏋️ {w}/{WORKOUTS_PER_WEEK} {week_progress(w, WORKOUTS_PER_WEEK)}\n"
                    f"💆 {b}/{BEAUTY_PER_WEEK} {week_progress(b, BEAUTY_PER_WEEK)}",
                    parse_mode="Markdown"
                )
                continue

            protein_eaten = d['protein']
            protein_left = max(0, PROTEIN_MIN - protein_eaten)
            protein_note = protein_status(protein_eaten)

            prompt = f"""Ты диетолог. Пользователь съел за день:
- Калории: {d['calories']} из {CALORIE_LIMIT} ккал (осталось {left} ккал)
- Белки: {protein_eaten:.0f} г (норма {PROTEIN_MIN:.0f}–{PROTEIN_RECOMMENDED:.0f} г, {'МАЛО — нужно добавить ~' + str(int(protein_left)) + ' г' if protein_left > 0 else 'норма выполнена'})
- Жиры: {d['fat']:.0f} г
- Углеводы: {d['carbs']:.0f} г

Предложи 3 варианта ужина которые:
1. Вписываются в оставшиеся {left} ккал
2. {'Содержат максимум белка (нужно добрать ~' + str(int(protein_left)) + ' г)' if protein_left > 0 else 'Сбалансированы по БЖУ'}
3. БЕЗ глютена, БЕЗ лактозы, БЕЗ добавленного сахара

Для каждого варианта укажи калории И белок.
Формат:
🍽 Вариант 1: [название] — ~[ккал] ккал | белок ~[г] г
[описание 1 строка]

🍽 Вариант 2: [название] — ~[ккал] ккал | белок ~[г] г
[описание 1 строка]

🍽 Вариант 3: [название] — ~[ккал] ккал | белок ~[г] г
[описание 1 строка]
Только по-русски."""
            await context.bot.send_message(
                user_id,
                f"🌆 *Время ужина!*\n\n"
                f"📊 Съедено: {d['calories']}/{CALORIE_LIMIT} ккал | Осталось: {left} ккал\n"
                f"{protein_note}\n\n"
                f"_(все варианты без глютена, лактозы и сахара)_\n\n"
                f"{suggestions}\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🏋️ {w}/{WORKOUTS_PER_WEEK} {week_progress(w, WORKOUTS_PER_WEEK)}\n"
                f"💆 {b}/{BEAUTY_PER_WEEK} {week_progress(b, BEAUTY_PER_WEEK)}",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning("Dinner notify failed for %s: %s", user_id, e)


# ── Запуск ───────────────────────────────────────────────────────────────────

async def notify_fridge(context: ContextTypes.DEFAULT_TYPE) -> None:
    """В 20:00 спрашивает что есть в холодильнике."""
    for user_id in context.bot_data.get("subscribers", set()):
        try:
            context.bot_data.setdefault("fridge_mode", set()).add(user_id)
            await context.bot.send_message(
                user_id,
                "🧊 *Что есть в холодильнике?*\n\n"
                "Напиши список продуктов — составлю меню на завтра на 1400 ккал "
                "без глютена, лактозы и сахара, с учётом нормы белка!\n\n"
                "Пример: _курица, яйца, огурцы, помидоры, рис, оливковое масло_",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning("Fridge notify failed for %s: %s", user_id, e)


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("vitamins", cmd_vitamins))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("fridge", cmd_fridge))
    app.add_handler(CallbackQueryHandler(handle_vitamin_callback, pattern="^vitamins_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    jq: JobQueue = app.job_queue

    # Вода каждые 2 часа 8:00–22:00
    for hour in range(8, 23, 2):
        jq.run_daily(notify_water, time=time(hour, 0, tzinfo=MSK))

    # Витамины каждые 3 часа с 10:00 до 22:00
    for hour in range(10, 23, 3):
        jq.run_daily(notify_vitamins_check, time=time(hour, 0, tzinfo=MSK))

    # Ужин в 17:00
    jq.run_daily(notify_dinner, time=time(17, 0, tzinfo=MSK))

    # Холодильник в 20:00
    jq.run_daily(notify_fridge, time=time(20, 0, tzinfo=MSK))

    logger.info("Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
