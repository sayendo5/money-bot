import asyncio
import csv
import io
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

TOKEN = os.getenv("BOT_TOKEN", "")
session = AiohttpSession(proxy=os.getenv("BOT_PROXY") or None)
DB_FILE = "money.db"

EXPENSE_CATEGORIES = {
    "food": "🍔 Еда",
    "transport": "🚕 Транспорт",
    "home": "🏠 Жильё",
    "phone": "📱 Связь",
    "fun": "🎮 Развлечения",
    "health": "💊 Здоровье",
    "shop": "🛍 Покупки",
    "other": "📦 Другое",
}

CATEGORY_ALIASES = {
    "еда": "food",
    "транспорт": "transport",
    "жильё": "home",
    "связь": "phone",
    "развлечения": "fun",
    "здоровье": "health",
    "покупки": "shop",
    "другое": "other",
}

router = Router()


class ExpenseForm(StatesGroup):
    waiting_amount = State()


class IncomeForm(StatesGroup):
    waiting_amount = State()


def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    with conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                note TEXT,
                created TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
    conn.close()


def init_subscriptions():
    conn = db_connect()
    with conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                amount REAL NOT NULL,
                day INTEGER NOT NULL
            )"""
        )
    conn.close()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add_transaction(ttype, amount, category, note):
    conn = db_connect()
    with conn:
        conn.execute(
            "INSERT INTO transactions (type, amount, category, note, created) VALUES (?, ?, ?, ?, ?)",
            (ttype, amount, category, note, now_str()),
        )
    conn.close()


def today_range():
    today = datetime.now().date()
    start = today.strftime("%Y-%m-%d")
    end = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    return start, end


def month_range():
    today = datetime.now().date()
    start = today.replace(day=1).strftime("%Y-%m-%d")
    if today.month == 12:
        end = today.replace(year=today.year + 1, month=1, day=1).strftime("%Y-%m-%d")
    else:
        end = today.replace(month=today.month + 1, day=1).strftime("%Y-%m-%d")
    return start, end


def period_totals(start, end):
    conn = db_connect()
    rows = conn.execute(
        "SELECT type, SUM(amount) AS total FROM transactions WHERE created >= ? AND created < ? GROUP BY type",
        (start, end),
    ).fetchall()
    conn.close()
    result = {"income": 0.0, "expense": 0.0}
    for row in rows:
        result[row["type"]] = row["total"] or 0.0
    return result


def category_totals(start, end):
    conn = db_connect()
    rows = conn.execute(
        "SELECT category, SUM(amount) AS total FROM transactions "
        "WHERE type = 'expense' AND created >= ? AND created < ? "
        "GROUP BY category ORDER BY SUM(amount) DESC",
        (start, end),
    ).fetchall()
    conn.close()
    return [(row["category"], row["total"]) for row in rows]


def set_setting(key, value):
    conn = db_connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
    conn.close()


def get_setting(key):
    conn = db_connect()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_budget(value):
    set_setting("budget", value)


def get_budget():
    value = get_setting("budget")
    return float(value) if value else None


def set_category_limit(category, value):
    set_setting("limit:" + category, value)


def get_category_limit(category):
    value = get_setting("limit:" + category)
    return float(value) if value else None


def add_subscription(name, amount, day):
    conn = db_connect()
    with conn:
        conn.execute(
            "INSERT INTO subscriptions (name, amount, day) VALUES (?, ?, ?)",
            (name, amount, day),
        )
    conn.close()


def get_subscriptions():
    conn = db_connect()
    rows = conn.execute("SELECT id, name, amount, day FROM subscriptions ORDER BY day").fetchall()
    conn.close()
    return rows


def delete_subscription(sub_id):
    conn = db_connect()
    with conn:
        conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    conn.close()


def fmt(value):
    if value == int(value):
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def parse_amount(text):
    parts = text.strip().replace(",", ".").split()
    if not parts:
        raise ValueError
    amount = round(float(parts[0]), 2)
    if amount <= 0:
        raise ValueError
    note = " ".join(parts[1:])[:200]
    return amount, note


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💸 Расход"), KeyboardButton(text="💰 Доход")],
            [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="📆 Месяц")],
            [KeyboardButton(text="📈 Статистика"), KeyboardButton(text="🎯 Лимиты")],
            [KeyboardButton(text="🔁 Подписки"), KeyboardButton(text="📤 Экспорт CSV")],
            [KeyboardButton(text="🗑 Удалить последнюю")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


def categories_keyboard():
    buttons = [
        InlineKeyboardButton(text=label, callback_data="cat:" + key)
        for key, label in EXPENSE_CATEGORIES.items()
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_summary(title, income, expense, categories):
    lines = [
        title,
        "",
        f"💰 Доход: {fmt(income)} ₽",
        f"💸 Расход: {fmt(expense)} ₽",
        f"🧮 Баланс: {fmt(income - expense)} ₽",
    ]
    if categories:
        lines.append("")
        lines.append("По категориям:")
        for category, total in categories:
            lines.append(f"{category}: {fmt(total)} ₽")
    if income == 0 and expense == 0:
        lines.append("Записей пока нет.")
    return "\n".join(lines)


def budget_status(expense):
    budget = get_budget()
    if not budget:
        return ""
    left = budget - expense
    if left < 0:
        return f"⚠️ Бюджет превышен на {fmt(-left)} ₽."
    percent = expense / budget * 100
    if percent >= 80:
        return f"⚠️ До лимита осталось {fmt(left)} ₽ из {fmt(budget)} ₽."
    return f"🎯 Бюджет: потрачено {fmt(expense)} из {fmt(budget)} ₽."


def category_limit_status(category, spent):
    key = next((k for k, v in EXPENSE_CATEGORIES.items() if v == category), category)
    limit = get_category_limit(key)
    if not limit:
        return ""
    if spent > limit:
        return f"⚠️ Лимит по категории {category} превышен: {fmt(spent)} из {fmt(limit)} ₽."
    if spent >= limit * 0.8:
        return f"⚠️ По категории {category} осталось {fmt(limit - spent)} ₽ до лимита {fmt(limit)} ₽."
    return ""


async def show_limits(message):
    start, end = month_range()
    totals = {cat: total for cat, total in category_totals(start, end)}
    lines = ["🎯 Лимиты по категориям:"]
    for key, label in EXPENSE_CATEGORIES.items():
        limit = get_category_limit(key)
        if limit:
            spent = totals.get(label, 0.0)
            lines.append(f"{label}: {fmt(spent)} из {fmt(limit)} ₽")
    if len(lines) == 1:
        await message.answer("Лимиты пока не заданы.\nПример: /limit еда 5000")
    else:
        await message.answer("\n".join(lines))


async def show_subs(message):
    rows = get_subscriptions()
    if not rows:
        await message.answer("Подписок пока нет.\nДобавить: /subs add Название 500 15")
        return
    lines = ["🔁 Подписки:"]
    for index, row in enumerate(rows, 1):
        lines.append(f"{index}. {row['name']} — {fmt(row['amount'])} ₽, {row['day']}-го числа")
    lines.append("Удалить: /subs del 1")
    await message.answer("\n".join(lines))


async def reminder_loop(bot):
    last_sent = ""
    while True:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        remind_time = get_setting("remind_time") or "21:00"
        chat_id = get_setting("chat_id")
        if chat_id and now.strftime("%H:%M") == remind_time and last_sent != today:
            try:
                message_text = "⏰ Вечерняя проверка! Не забудь записать расходы за день."
                subs_today = [f"{row['name']} — {fmt(row['amount'])} ₽" for row in get_subscriptions() if row["day"] == now.day]
                if subs_today:
                    message_text += "\n\nСегодня списываются подписки:\n" + "\n".join(subs_today)
                await bot.send_message(int(chat_id), message_text)
                last_sent = today
            except Exception:
                pass
        await asyncio.sleep(30)


@router.message(Command("start"))
async def cmd_start(message: Message):
    set_setting("chat_id", message.chat.id)
    if not get_setting("remind_time"):
        set_setting("remind_time", "21:00")
    await message.answer(
        "Привет! Я бот «Деньги под контролем» 💵\n\n"
        "💸 Расход — записать трату\n"
        "💰 Доход — записать поступление\n"
        "📊 Сегодня — итоги за день\n"
        "📆 Месяц — итоги за месяц\n"
        "📈 Статистика — средний расход и топ категорий\n"
        "🎯 Лимиты — лимиты по категориям\n"
        "🔁 Подписки — регулярные платежи\n\n"
        "Команды:\n"
        "/budget 30000 — бюджет на месяц\n"
        "/limit еда 5000 — лимит на категорию\n"
        "/subs add Netflix 499 15 — добавить подписку\n"
        "/remind 21:00 — время напоминания",
        reply_markup=main_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Как записать расход:\n"
        "1. Нажми «💸 Расход»\n"
        "2. Выбери категорию\n"
        "3. Напиши сумму и комментарий, например: 350 кофе\n\n"
        "Как записать доход:\n"
        "Нажми «💰 Доход» и напиши: 5000 подработка\n\n"
        "Лимиты: /limit еда 5000\n"
        "Бюджет: /budget 30000\n"
        "Статистика: /stats\n"
        "Подписки: /subs\n"
        "Напоминание: /remind 21:00"
    )


@router.message(Command("budget"))
async def cmd_budget(message: Message):
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 1:
        budget = get_budget()
        if budget:
            start, end = month_range()
            expense = period_totals(start, end)["expense"]
            left = budget - expense
            await message.answer(
                f"Бюджет на месяц: {fmt(budget)} ₽\n"
                f"Потрачено: {fmt(expense)} ₽\n"
                f"Осталось: {fmt(left)} ₽"
            )
        else:
            await message.answer("Бюджет ещё не задан.\nПример: /budget 30000")
    else:
        try:
            value = float(parts[1].replace(",", "."))
            if value <= 0:
                raise ValueError
            set_budget(value)
            await message.answer(f"Бюджет на месяц установлен: {fmt(value)} ₽")
        except ValueError:
            await message.answer("Не понял сумму. Пример: /budget 30000")


@router.message(F.text == "💸 Расход")
async def expense_start(message: Message, state: FSMContext):
    await state.set_state(ExpenseForm.waiting_amount)
    await message.answer("Выбери категорию:", reply_markup=categories_keyboard())


@router.callback_query(ExpenseForm.waiting_amount, F.data.startswith("cat:"))
async def expense_category(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    label = EXPENSE_CATEGORIES.get(key)
    if not label:
        await callback.answer("Категория не найдена")
        return
    await state.update_data(category=label)
    await callback.message.edit_text(
        f"Категория: {label}\n\n"
        "Напиши сумму и комментарий через пробел:\n"
        "350 кофе"
    )
    await callback.answer()


@router.message(ExpenseForm.waiting_amount)
async def expense_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    category = data.get("category")
    if not category:
        await state.clear()
        await message.answer("Сначала нажми «💸 Расход».")
        return
    try:
        amount, note = parse_amount(message.text)
    except (ValueError, IndexError):
        await message.answer("Не понял сумму. Пример: 350 кофе")
        return
    add_transaction("expense", amount, category, note)
    await state.clear()
    start, end = month_range()
    expense = period_totals(start, end)["expense"]
    answer = f"✅ Записал расход: {fmt(amount)} ₽ • {category}"
    if note:
        answer += f" ({note})"
    status = budget_status(expense)
    if status:
        answer += "\n\n" + status
    spent = next((total for cat, total in category_totals(start, end) if cat == category), 0.0)
    cat_status = category_limit_status(category, spent)
    if cat_status:
        answer += "\n\n" + cat_status
    await message.answer(answer)


@router.message(F.text == "💰 Доход")
async def income_start(message: Message, state: FSMContext):
    await state.set_state(IncomeForm.waiting_amount)
    await message.answer("Напиши сумму дохода и комментарий через пробел:\n5000 подработка")


@router.message(IncomeForm.waiting_amount)
async def income_amount(message: Message, state: FSMContext):
    try:
        amount, note = parse_amount(message.text)
    except (ValueError, IndexError):
        await message.answer("Не понял сумму. Пример: 5000 подработка")
        return
    add_transaction("income", amount, "💰 Доход", note)
    await state.clear()
    answer = f"✅ Записал доход: {fmt(amount)} ₽"
    if note:
        answer += f" ({note})"
    await message.answer(answer)


@router.message(F.text == "📊 Сегодня")
async def today_report(message: Message):
    start, end = today_range()
    totals = period_totals(start, end)
    categories = category_totals(start, end)
    await message.answer(build_summary("📊 Итоги за сегодня", totals["income"], totals["expense"], categories))


@router.message(F.text == "📆 Месяц")
async def month_report(message: Message):
    start, end = month_range()
    totals = period_totals(start, end)
    categories = category_totals(start, end)
    text = build_summary("📆 Итоги за месяц", totals["income"], totals["expense"], categories)
    status = budget_status(totals["expense"])
    if status:
        text += "\n\n" + status
    await message.answer(text)


@router.message(F.text == "🗑 Удалить последнюю")
async def delete_last(message: Message):
    conn = db_connect()
    row = conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        conn.close()
        await message.answer("Записей пока нет.")
        return
    with conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
    conn.close()
    label = "доход" if row["type"] == "income" else "расход"
    await message.answer(f"🗑 Удалено: {label} {fmt(row['amount'])} ₽ • {row['category']}")


@router.message(F.text == "📤 Экспорт CSV")
async def export_csv(message: Message):
    conn = db_connect()
    rows = conn.execute("SELECT created, type, category, amount, note FROM transactions ORDER BY created").fetchall()
    conn.close()
    if not rows:
        await message.answer("Записей пока нет — экспортировать нечего.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Дата", "Тип", "Категория", "Сумма", "Комментарий"])
    for row in rows:
        writer.writerow([
            row["created"],
            "Доход" if row["type"] == "income" else "Расход",
            row["category"],
            str(row["amount"]).replace(".", ","),
            row["note"] or "",
        ])
    content = "\uFEFF" + buf.getvalue()
    await message.answer_document(
        BufferedInputFile(content.encode("utf-8"), filename="money_export.csv"),
        caption="Твои финансы. Открывается в Excel или Google Таблицах.",
    )


@router.message(Command("remind"))
async def cmd_remind(message: Message):
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 1:
        current = get_setting("remind_time") or "21:00"
        await message.answer(f"⏰ Напоминание стоит на {current}. Изменить: /remind 21:30")
    else:
        value = parts[1].strip()
        try:
            datetime.strptime(value, "%H:%M")
            set_setting("remind_time", value)
            set_setting("chat_id", message.chat.id)
            await message.answer(f"⏰ Напоминание установлено на {value}.")
        except ValueError:
            await message.answer("Не понял время. Формат: ЧЧ:ММ. Пример: /remind 21:00")


@router.message(Command("limit"))
async def cmd_limit(message: Message):
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) == 1:
        await show_limits(message)
        return
    alias = parts[1].strip().lower()
    key = CATEGORY_ALIASES.get(alias)
    if not key:
        await message.answer("Категория не найдена. Доступно: еда, транспорт, жильё, связь, развлечения, здоровье, покупки, другое")
        return
    try:
        value = float(parts[2].replace(",", "."))
        if value <= 0:
            raise ValueError
    except (ValueError, IndexError):
        await message.answer("Формат: /limit еда 5000")
        return
    set_category_limit(key, value)
    await message.answer(f"Лимит установлен: {EXPENSE_CATEGORIES[key]} — {fmt(value)} ₽ в месяц.")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    start, end = month_range()
    totals = period_totals(start, end)
    cats = category_totals(start, end)
    days = datetime.now().day
    avg = totals["expense"] / days if days else 0.0
    text = build_summary("📈 Статистика за месяц", totals["income"], totals["expense"], cats)
    text += "\n\nСредний расход в день: " + fmt(avg) + " ₽"
    if cats:
        text += "\nСамая затратная категория: " + cats[0][0] + " — " + fmt(cats[0][1]) + " ₽"
    status = budget_status(totals["expense"])
    if status:
        text += "\n" + status
    await message.answer(text)


@router.message(Command("subs"))
async def cmd_subs(message: Message):
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) == 1:
        await show_subs(message)
        return
    action = parts[1].strip().lower()
    if action == "add":
        try:
            pieces = parts[2].rsplit(maxsplit=2)
            if len(pieces) != 3:
                raise ValueError
            name = pieces[0].strip()
            amount = float(pieces[1].replace(",", "."))
            day = int(pieces[2])
            if amount <= 0 or day < 1 or day > 31:
                raise ValueError
        except (ValueError, IndexError):
            await message.answer("Формат: /subs add Название 500 15")
            return
        add_subscription(name, amount, day)
        await message.answer(f"Подписка добавлена: {name} — {fmt(amount)} ₽, {day}-го числа.")
    elif action == "del":
        try:
            num = int(parts[2])
        except (ValueError, IndexError):
            await message.answer("Формат: /subs del 1")
            return
        rows = get_subscriptions()
        if num < 1 or num > len(rows):
            await message.answer("Нет подписки с таким номером.")
            return
        delete_subscription(rows[num - 1]["id"])
        await message.answer("Подписка удалена.")
    else:
        await message.answer("Команды: /subs, /subs add Название 500 15, /subs del 1")


@router.message(F.text == "📈 Статистика")
async def stats_button(message: Message):
    await cmd_stats(message)


@router.message(F.text == "🎯 Лимиты")
async def limits_button(message: Message):
    await show_limits(message)


@router.message(F.text == "🔁 Подписки")
async def subs_button(message: Message):
    await show_subs(message)


async def set_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="budget", description="Бюджет на месяц"),
        BotCommand(command="limit", description="Лимиты по категориям"),
        BotCommand(command="stats", description="Статистика за месяц"),
        BotCommand(command="subs", description="Подписки"),
        BotCommand(command="remind", description="Напоминание"),
        BotCommand(command="help", description="Как пользоваться"),
    ])


async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    init_subscriptions()
    bot = Bot(token=TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await set_commands(bot)
    asyncio.create_task(reminder_loop(bot))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, polling_timeout=20)


if __name__ == "__main__":
    asyncio.run(main())