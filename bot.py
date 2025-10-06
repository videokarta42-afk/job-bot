# bot.py
# -*- coding: utf-8 -*-
# Требования: aiogram>=3.10,<4.0  (совместимо с Python 3.13)
# Установка: pip install "aiogram>=3.10,<4.0"
# Запуск:    python bot.py

import asyncio
import logging
import sqlite3
from datetime import datetime, UTC

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

# =======================
# ⚙️ КОНФИГ ПОЛЬЗОВАТЕЛЯ
# =======================
BOT_TOKEN = "8338911717:AAEqkGso9HnXw3hd8W2Hn4gr7iOdag35838"
ADMIN_ID = 8143233139  # ID администратора
CHANNEL_ID = -1003167742288  # ID канала (целое число)

# Настройки оплаты — ТОЛЬКО ТИНЬКОФФ
DEFAULT_PRICE = "500 ₽"  # изначальная стоимость размещения (админ может менять)
PAYMENT_DETAILS = """
💳 Реквизиты для оплаты:
🏦 Тинькофф: 2200701966220961
(Матвей М.)
"""

# Быстрые причины отклонения (код -> человекочитаемый текст)
REJECT_PRESETS = {
    "casino": "Казино/ставки",
    "18plus": "Контент 18+",
    "fraud": "Подозрение на мошенничество",
    "abuse": "Нецензурная/оскорбительная информация",
    "duplicate": "Дубликат/спам",
    "badpay": "Некорректные/отсутствующие реквизиты",
}

# ===============
# 🔧 ИНИЦИАЛИЗАЦИЯ
# ===============
logging.basicConfig(level=logging.INFO)

# ===================
# 💾 БАЗА ДАННЫХ (SQLite)
# ===================
DB_PATH = "bot_data.sqlite3"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS vacancies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        created_at TEXT,
        receipt_msg_id INTEGER,
        receipt_text TEXT,
        title TEXT,
        pay TEXT,
        description TEXT,
        contact TEXT,
        status TEXT,            -- draft|pending|approved|rejected|posted
        admin_comment TEXT
    )
    """)
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('price', ?)", (DEFAULT_PRICE,))
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('payment_details', ?)", (PAYMENT_DETAILS,))
    conn.commit()
    conn.close()

def get_setting(key, default=""):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()

def create_vacancy(user_id, username):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO vacancies (user_id, username, created_at, status)
        VALUES (?, ?, ?, 'draft')
    """, (user_id, username or "", datetime.now(UTC).isoformat()))
    vid = cur.lastrowid
    conn.commit()
    conn.close()
    return vid

def update_vacancy(vid, **kwargs):
    if not kwargs:
        return
    fields = ", ".join([f"{k}=?" for k in kwargs.keys()])
    values = list(kwargs.values()) + [vid]
    conn = db()
    cur = conn.cursor()
    cur.execute(f"UPDATE vacancies SET {fields} WHERE id=?", values)
    conn.commit()
    conn.close()

def get_vacancy(vid):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM vacancies WHERE id=?", (vid,))
    row = cur.fetchone()
    conn.close()
    return row

# ==========================
# 🧭 СОСТОЯНИЯ (FSM)
# ==========================
class CreateFlow(StatesGroup):
    WaitingReceipt = State()
    WaitingTitle = State()
    WaitingPay = State()
    WaitingDescription = State()
    WaitingContact = State()
    WaitingUserConfirm = State()

class AdminEdit(StatesGroup):
    WaitingNewPrice = State()
    WaitingNewDetails = State()
    WaitingRejectReason = State()  # ручной ввод причины

# ==========================
# 🧩 УТИЛИТЫ/КНОПКИ
# ==========================
def start_kb_and_caption():
    price = get_setting("price", DEFAULT_PRICE)
    kb = InlineKeyboardBuilder()
    kb.button(text="Я оплатил(а)", callback_data="paid")
    kb.button(text="Как отправить чек?", callback_data="how_receipt")
    kb.button(text="Сделать новый пост", callback_data="new_post")
    kb.button(text="Админ-панель", callback_data="admin_panel")
    kb.adjust(1, 1, 1, 1)
    caption = f"Стоимость размещения: <b>{price}</b>\n\n{get_setting('payment_details', PAYMENT_DETAILS)}"
    return kb.as_markup(), caption

def admin_panel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Сменить цену", callback_data="admin_set_price")
    kb.button(text="Сменить реквизиты", callback_data="admin_set_details")
    kb.adjust(1, 1)
    return kb.as_markup()

def approval_kb(vacancy_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"approve:{vacancy_id}")
    kb.button(text="❌ Отклонить", callback_data=f"reject:{vacancy_id}")
    kb.adjust(2)
    return kb.as_markup()

def reject_reasons_kb(vacancy_id: int):
    kb = InlineKeyboardBuilder()
    # пресеты в две строки
    kb.button(text="Казино/ставки", callback_data=f"reject_reason:{vacancy_id}:casino")
    kb.button(text="Контент 18+", callback_data=f"reject_reason:{vacancy_id}:18plus")
    kb.button(text="Мошенничество", callback_data=f"reject_reason:{vacancy_id}:fraud")
    kb.button(text="Нецензурное", callback_data=f"reject_reason:{vacancy_id}:abuse")
    kb.button(text="Дубликат/спам", callback_data=f"reject_reason:{vacancy_id}:duplicate")
    kb.button(text="Некорректные реквизиты", callback_data=f"reject_reason:{vacancy_id}:badpay")
    kb.adjust(2, 2, 2)
    # прочее / отмена
    kb.button(text="Другая причина…", callback_data=f"reject_other:{vacancy_id}")
    kb.button(text="Отмена", callback_data=f"reject_cancel:{vacancy_id}")
    kb.adjust(2)
    return kb.as_markup()

def yes_no_kb(vacancy_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="Отправить на модерацию ✅", callback_data=f"user_send:{vacancy_id}")
    kb.button(text="Изменить ✏️", callback_data=f"user_edit:{vacancy_id}")
    kb.button(text="Отмена 🚫", callback_data=f"user_cancel:{vacancy_id}")
    kb.adjust(1, 2)
    return kb.as_markup()

def format_vacancy_post(row: sqlite3.Row):
    return (
        f"💼 <b>Вакансия:</b> {row['title']}\n"
        f"💰 <b>Оплата:</b> {row['pay']}\n"
        f"📋 <b>Описание:</b> {row['description']}\n"
        f"📩 <b>Контакт:</b> {row['contact']}"
    )

# ==========================
# 🧠 РОУТЕРЫ И ХЕНДЛЕРЫ
# ==========================
router = Router()

@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    kb, caption = start_kb_and_caption()
    greeting = (
        "Привет! Я бот биржи вакансий.\n\n"
        "Как это работает:\n"
        "1) Оплачиваете размещение.\n"
        "2) Отправляете чек.\n"
        "3) Заполняете данные вакансии.\n"
        "4) Админ подтверждает — и пост публикуется в канале."
    )
    await message.answer(greeting)
    await message.answer(caption, reply_markup=kb)

@router.callback_query(F.data == "how_receipt")
async def cb_how_receipt(call: types.CallbackQuery):
    await call.answer()
    text = (
        "Отправьте скрин/фото чека или укажите текстом: сумма, последние 4 цифры карты, дата/время.\n"
        "После чека бот спросит данные вакансии."
    )
    await call.message.answer(text)

@router.callback_query(F.data == "new_post")
async def cb_new_post(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    vid = create_vacancy(call.from_user.id, call.from_user.username)
    await state.update_data(vacancy_id=vid)
    await state.set_state(CreateFlow.WaitingReceipt)
    price = get_setting("price", DEFAULT_PRICE)
    await call.message.answer(
        f"Создан черновик #{vid}.\nСначала пришлите чек об оплате (<b>{price}</b>). "
        "Это может быть фото/скриншот или текст с деталями платежа."
    )

@router.callback_query(F.data == "paid")
async def cb_paid(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    vid = create_vacancy(call.from_user.id, call.from_user.username)
    await state.update_data(vacancy_id=vid)
    await state.set_state(CreateFlow.WaitingReceipt)
    await call.message.answer(
        f"Отлично! Создан черновик #{vid}.\nПришлите, пожалуйста, чек (фото/скрин) или укажите данные платежа."
    )

@router.message(CreateFlow.WaitingReceipt, F.content_type.in_({"photo", "text", "document"}))
async def on_receipt(message: types.Message, state: FSMContext):
    data = await state.get_data()
    vid = data.get("vacancy_id")
    if not vid:
        vid = create_vacancy(message.from_user.id, message.from_user.username)
        await state.update_data(vacancy_id=vid)

    receipt_text = None
    receipt_msg_id = None

    if message.photo:
        receipt_msg_id = message.message_id
    else:
        receipt_text = (message.text or message.caption or "").strip() or "(без текста)"

    update_vacancy(vid, receipt_msg_id=receipt_msg_id, receipt_text=receipt_text)
    await state.set_state(CreateFlow.WaitingTitle)
    await message.answer("✅ Чек получен. Укажите <b>Название вакансии</b>:")

@router.message(CreateFlow.WaitingTitle, F.text)
async def on_title(message: types.Message, state: FSMContext):
    vid = (await state.get_data()).get("vacancy_id")
    update_vacancy(vid, title=message.text.strip())
    await state.set_state(CreateFlow.WaitingPay)
    await message.answer("Отлично! Укажите <b>Оплату</b> (сумма/диапазон/условия):")

@router.message(CreateFlow.WaitingPay, F.text)
async def on_pay(message: types.Message, state: FSMContext):
    vid = (await state.get_data()).get("vacancy_id")
    update_vacancy(vid, pay=message.text.strip())
    await state.set_state(CreateFlow.WaitingDescription)
    await message.answer("Принято. Напишите <b>Описание</b> вакансии:")

@router.message(CreateFlow.WaitingDescription, F.text)
async def on_description(message: types.Message, state: FSMContext):
    vid = (await state.get_data()).get("vacancy_id")
    update_vacancy(vid, description=message.text.strip())
    await state.set_state(CreateFlow.WaitingContact)
    await message.answer("Хорошо. Укажите <b>контакт</b> (Telegram @ник, email, форма и т.п.):")

@router.message(CreateFlow.WaitingContact, F.text)
async def on_contact(message: types.Message, state: FSMContext):
    vid = (await state.get_data()).get("vacancy_id")
    update_vacancy(vid, contact=message.text.strip())
    row = get_vacancy(vid)
    preview = "Вот как будет выглядеть пост:\n\n" + format_vacancy_post(row)
    await message.answer(preview, reply_markup=yes_no_kb(vid))
    await state.set_state(CreateFlow.WaitingUserConfirm)

@router.callback_query(CreateFlow.WaitingUserConfirm, F.data.startswith("user_send:"))
async def cb_user_send(call: types.CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    vid = int(call.data.split(":")[1])
    update_vacancy(vid, status="pending")
    row = get_vacancy(vid)

    text = (
        f"📝 <b>На модерацию</b> (#{vid}) от @{row['username'] or call.from_user.id}\n\n"
        + format_vacancy_post(row)
        + "\n\nЧек: "
        + ("фото (перешлём следом)" if row["receipt_msg_id"] else (row["receipt_text"] or "нет"))
    )
    await bot.send_message(ADMIN_ID, text, reply_markup=approval_kb(vid))

    if row["receipt_msg_id"]:
        try:
            await bot.forward_message(ADMIN_ID, call.message.chat.id, row["receipt_msg_id"])
        except Exception:
            pass

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer("✅ Отправлено на модерацию. Ожидайте решения администратора.")
    await state.clear()

@router.callback_query(CreateFlow.WaitingUserConfirm, F.data.startswith("user_edit:"))
async def cb_user_edit(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    vid = int(call.data.split(":")[1])
    await call.message.answer(
        f"Что хотите изменить?\n"
        f"- Отправьте заново нужное поле по порядку.\n"
        f"Сейчас перезапрос начнётся с <b>Название вакансии</b>."
    )
    await state.update_data(vacancy_id=vid)
    await state.set_state(CreateFlow.WaitingTitle)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

@router.callback_query(CreateFlow.WaitingUserConfirm, F.data.startswith("user_cancel:"))
async def cb_user_cancel(call: types.CallbackQuery, state: FSMContext):
    await call.answer("Черновик оставлен без изменений.")
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

# ================
# 👨‍✈️ АДМИН ПАНЕЛЬ
# ================
@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: types.CallbackQuery):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        await call.message.answer("⛔ Доступно только администратору.")
        return
    price = get_setting("price", DEFAULT_PRICE)
    details = get_setting("payment_details", PAYMENT_DETAILS)
    await call.message.answer(
        f"<b>Админ-панель</b>\nТекущая цена: <b>{price}</b>\n\nТекущие реквизиты:\n{details}",
        reply_markup=admin_panel_kb()
    )

@router.callback_query(F.data == "admin_set_price")
async def cb_admin_set_price(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        await call.message.answer("⛔ Только администратор.")
        return
    await state.set_state(AdminEdit.WaitingNewPrice)
    await call.message.answer("Введите новую цену (например: 700 ₽):")

@router.message(AdminEdit.WaitingNewPrice, F.text)
async def on_admin_new_price(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только администратор.")
        return
    set_setting("price", message.text.strip())
    await state.clear()
    await message.answer(f"✅ Цена обновлена: <b>{message.text.strip()}</b>")

@router.callback_query(F.data == "admin_set_details")
async def cb_admin_set_details(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        await call.message.answer("⛔ Только администратор.")
        return
    await state.set_state(AdminEdit.WaitingNewDetails)
    await call.message.answer(
        "Отправьте новые реквизиты (текстом). Пример:\n"
        "💳 Реквизиты для оплаты:\n🏦 Тинькофф: 2200...\n(Имя Ф.)"
    )

@router.message(AdminEdit.WaitingNewDetails, F.text)
async def on_admin_new_details(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только администратор.")
        return
    text = message.text.strip()
    set_setting("payment_details", text)
    await state.clear()
    await message.answer("✅ Реквизиты обновлены.")

# ======================
# ✅/❌ МОДЕРАЦИЯ АДМИНА
# ======================
@router.callback_query(F.data.startswith("approve:"))
async def cb_admin_approve(call: types.CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Только администратор.", show_alert=True)
        return
    _, sid = call.data.split(":")
    vid = int(sid)
    row = get_vacancy(vid)
    if not row:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    post_text = format_vacancy_post(row)
    try:
        msg = await bot.send_message(CHANNEL_ID, post_text, disable_web_page_preview=True)
        update_vacancy(vid, status="posted", admin_comment="approved")
        try:
            await call.message.edit_text((call.message.text or "") + "\n\n✅ <b>Опубликовано</b>")
        except Exception:
            await call.message.answer("✅ Опубликовано")
        try:
            await bot.send_message(row["user_id"], f"✅ Ваша вакансия опубликована: {msg.chat.title}")
        except Exception:
            pass
    except Exception as e:
        update_vacancy(vid, status="approved", admin_comment=f"approve_error: {e}")
        await call.message.answer("⚠️ Не удалось опубликовать в канал. Проверьте права бота (постинг в канал).")

@router.callback_query(F.data.startswith("reject:"))
async def cb_admin_reject_menu(call: types.CallbackQuery, state: FSMContext):
    """Показать быстрые причины + кнопки 'Другая причина…' и 'Отмена'."""
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Только администратор.", show_alert=True)
        return
    _, sid = call.data.split(":")
    vid = int(sid)
    if not get_vacancy(vid):
        await call.answer("Заявка не найдена.", show_alert=True)
        return
    await state.update_data(reject_vid=vid, mod_message_id=call.message.message_id, mod_chat_id=call.message.chat.id)
    await call.message.answer(f"Выберите причину отклонения для заявки #{vid}:", reply_markup=reject_reasons_kb(vid))
    await call.answer()

@router.callback_query(F.data.startswith("reject_cancel:"))
async def cb_admin_reject_cancel(call: types.CallbackQuery, state: FSMContext):
    """Отмена процесса отклонения."""
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Только администратор.", show_alert=True)
        return
    await state.update_data(reject_vid=None, mod_message_id=None, mod_chat_id=None)
    await call.answer("Отменено.")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

@router.callback_query(F.data.startswith("reject_other:"))
async def cb_admin_reject_other(call: types.CallbackQuery, state: FSMContext):
    """Запросить ручной ввод причины."""
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Только администратор.", show_alert=True)
        return
    _, sid = call.data.split(":")
    vid = int(sid)
    await state.update_data(reject_vid=vid, mod_message_id=call.message.message_id, mod_chat_id=call.message.chat.id)
    await state.set_state(AdminEdit.WaitingRejectReason)
    await call.message.answer(f"📝 Введите свою причину отклонения для заявки #{vid} одним сообщением.")
    await call.answer()

@router.callback_query(F.data.startswith("reject_reason:"))
async def cb_admin_reject_preset(call: types.CallbackQuery, bot: Bot, state: FSMContext):
    """Обработка выбора пресетной причины."""
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Только администратор.", show_alert=True)
        return
    _, sid, code = call.data.split(":")
    vid = int(sid)
    row = get_vacancy(vid)
    if not row:
        await call.answer("Заявка не найдена.", show_alert=True)
        return
    reason = REJECT_PRESETS.get(code, "Причина не указана")
    update_vacancy(vid, status="rejected", admin_comment=reason)

    # уведомляем автора
    try:
        await bot.send_message(row["user_id"], f"❌ <b>Ваш пост не прошёл проверку</b>\nПричина: {reason}")
    except Exception:
        pass

    # обновляем карточку модерации
    try:
        await call.message.edit_text((call.message.text or "") + f"\n\n❌ <b>Отклонено</b>\nПричина: {reason}")
    except Exception:
        await call.message.answer(f"❌ Отклонено #{vid}\nПричина: {reason}")

    await call.answer("Отклонено.")
    await state.clear()

@router.message(AdminEdit.WaitingRejectReason, F.text)
async def on_admin_reject_reason(message: types.Message, state: FSMContext, bot: Bot):
    """Ручной ввод причины, отклоняем и уведомляем автора."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только администратор.")
        return

    data = await state.get_data()
    vid = int(data.get("reject_vid"))
    row = get_vacancy(vid)
    if not row:
        await state.clear()
        await message.answer("Заявка не найдена.")
        return

    reason = message.text.strip() or "Причина не указана"
    update_vacancy(vid, status="rejected", admin_comment=reason)

    # 1) Уведомляем автора
    try:
        await bot.send_message(row["user_id"], f"❌ <b>Ваш пост не прошёл проверку</b>\nПричина: {reason}")
    except Exception:
        pass

    # 2) Обновляем модерационную карточку (попробуем редактировать последнее сообщение админа)
    try:
        await bot.edit_message_text(
            chat_id=data.get("mod_chat_id"),
            message_id=data.get("mod_message_id"),
            text=(f"📝 <b>На модерацию</b> (#{vid}) от @{row['username'] or row['user_id']}\n\n"
                  + format_vacancy_post(row)
                  + f"\n\n❌ <b>Отклонено</b>\nПричина: {reason}")
        )
    except Exception:
        await message.answer(f"❌ Отклонено #{vid}\nПричина: {reason}")

    await message.answer(f"❌ Заявка #{vid} отклонена. Причина отправлена пользователю.")
    await state.clear()

# ======================
# 🏁 /help и дефолты
# ======================
@router.message(Command("help"))
async def cmd_help(message: types.Message):
    kb, caption = start_kb_and_caption()
    await message.answer(
        "Команды:\n"
        "/start — начало работы\n"
        "/help — справка\n\n"
        "Процесс: оплата → чек → ввод данных → модерация → публикация.",
    )
    await message.answer(caption, reply_markup=kb)

@router.message()
async def fallback(message: types.Message):
    if message.text and message.text.startswith("/"):
        return
    kb, _ = start_kb_and_caption()
    await message.answer("Не понял. Нажмите «Я оплатил(а)» или «Сделать новый пост».", reply_markup=kb)

# ===============
# 🚀 MAIN
# ===============
async def main():
    init_db()
    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
