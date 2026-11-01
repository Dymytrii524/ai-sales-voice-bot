import html
import os
import threading
import time
from collections import defaultdict

import anthropic
import telebot
from telebot import types

from config import API_TOKEN

try:
    from config import ANTHROPIC_API_KEY
except ImportError:
    ANTHROPIC_API_KEY = None

if ANTHROPIC_API_KEY:
    os.environ.setdefault("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)

import assistant  # noqa: E402  (needs the API key in the environment first)
import limits     # noqa: E402
import phone      # noqa: E402
import sheets     # noqa: E402
import webcall    # noqa: E402
from assistant import TEXTS  # noqa: E402

STATUS_CHECK_SECONDS = 60  # how often the "Статус обробки" column in Google Sheets is checked

phone.app.include_router(webcall.router)  # the free Telegram call runs on the same server

bot = telebot.TeleBot(token=API_TOKEN)

# ---------------------------------------------------------------------------
# /start, /catalog, /about
# ---------------------------------------------------------------------------


@bot.message_handler(commands=['start'])
def welcome(message):
    bot.send_message(message.chat.id, TEXTS["welcome"])


@bot.message_handler(commands=['catalog'])
def send_catalog(message):
    send_categories(message.chat.id)


def send_categories(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[
        types.InlineKeyboardButton(c, callback_data=f"cat:{i}")
        for i, c in enumerate(assistant.CATEGORIES)
    ])
    bot.send_message(chat_id, TEXTS["catalog_header"], reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "catalog")
def back_to_categories(call):
    bot.answer_callback_query(call.id)
    send_categories(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("cat:"))
def send_category(call):
    """The category's products as buttons - tap one to see its photo card."""
    bot.answer_callback_query(call.id)
    category = assistant.CATEGORIES[int(call.data[4:])]
    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in assistant.CATALOG:
        if p["category"] == category:
            mark = "" if assistant.in_stock(p) else " ⏳"
            label = f"{p['title'][:45]} - {float(p['price_uah']):g} грн{mark}"
            markup.add(types.InlineKeyboardButton(label, callback_data=f"prod:{p['id']}"))
    markup.add(types.InlineKeyboardButton(TEXTS["catalog_back_button"], callback_data="catalog"))
    bot.send_message(call.message.chat.id, f"🌱 {category}:", reply_markup=markup)


photo_cache = {}  # product id -> Telegram file_id (each photo is uploaded only once)


@bot.callback_query_handler(func=lambda call: call.data.startswith("prod:"))
def send_product(call):
    bot.answer_callback_query(call.id)
    product = next((p for p in assistant.CATALOG if p["id"] == call.data[5:]), None)
    if not product:
        return
    caption = product_caption(product)
    markup = product_buttons(product)
    chat_id = call.message.chat.id
    photo = photo_cache.get(product["id"]) or product["photo"]
    if photo:
        try:
            sent = bot.send_photo(chat_id, photo, caption=caption, parse_mode="HTML", reply_markup=markup)
            photo_cache[product["id"]] = sent.photo[-1].file_id
            return
        except Exception as e:
            print(f"Could not send photo of product {product['id']}: {e}")
    bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=markup)


def product_caption(p):
    stock = "✅ в наявності" if assistant.in_stock(p) else "⏳ немає в наявності, можна під замовлення"
    lines = [f"<b>{html.escape(p['title'])}</b>", "",
             f"💰 {html.escape(assistant.format_price(p))}", stock]
    features = [f for f in p["features"].split("; ") if f][:5]
    if features:
        lines += [""] + [f"• {html.escape(f)}" for f in features]
    return "\n".join(lines)[:1024]  # Telegram's limit for photo captions


def product_buttons(p):
    markup = types.InlineKeyboardMarkup(row_width=2)
    row = []
    if phone.PUBLIC_URL:
        row.append(types.InlineKeyboardButton(
            TEXTS["product_call_button"], web_app=types.WebAppInfo(f"{phone.PUBLIC_URL}/webcall")))
    row.append(types.InlineKeyboardButton(TEXTS["product_chat_button"], callback_data=f"ask:{p['id']}"))
    markup.add(*row)
    if p["url"]:
        markup.add(types.InlineKeyboardButton(TEXTS["product_site_button"], url=p["url"]))
    return markup


@bot.message_handler(commands=['about'])
def send_about(message):
    bot.send_message(message.chat.id, assistant.about_text())

# ---------------------------------------------------------------------------
# /call - free voice call with the AI inside Telegram (webcall.py)
# ---------------------------------------------------------------------------


@bot.message_handler(commands=['call'])
def open_webcall(message):
    if not phone.PUBLIC_URL:
        bot.send_message(message.chat.id, TEXTS["call_not_configured"])
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        TEXTS["webcall_button"], web_app=types.WebAppInfo(f"{phone.PUBLIC_URL}/webcall")))
    bot.send_message(message.chat.id, TEXTS["webcall_invite"], reply_markup=markup)

# ---------------------------------------------------------------------------
# Real phone calls through Twilio (paid) - only when TWILIO_* is set in config.py
# ---------------------------------------------------------------------------


@bot.message_handler(content_types=['contact'])
def call_customer(message):
    contact = message.contact
    if contact.user_id != message.from_user.id:  # only your own number
        bot.send_message(message.chat.id, TEXTS["call_share_phone"])
        return
    number = contact.phone_number if contact.phone_number.startswith("+") else "+" + contact.phone_number
    try:
        phone.start_outbound_call(number, message.chat.id, message.from_user.username)
    except Exception as e:
        print(f"Failed to start call to chat {message.chat.id}: {e}")
        bot.send_message(message.chat.id, TEXTS["call_failed"], reply_markup=types.ReplyKeyboardRemove())
        return
    bot.send_message(message.chat.id, TEXTS["call_calling"], reply_markup=types.ReplyKeyboardRemove())


def notify_call_saved(chat_id, data):
    bot.send_message(chat_id, TEXTS["call_saved"])


phone.on_call_saved = notify_call_saved
webcall.on_call_saved = notify_call_saved

# ---------------------------------------------------------------------------
# /chat - the same AI manager in text messages
# ---------------------------------------------------------------------------

active_chats = {}  # chat_id -> list of {"role", "content"}


def finish_chat(message):
    history = active_chats.pop(message.chat.id, None)
    data = assistant.finish_conversation(
        history, "telegram",
        chat_id=message.chat.id, username=message.from_user.username,
    )
    if data and data["status"] == "order":
        confirm_by_phone(message, data)
    return data


def confirm_by_phone(message, order):
    """A new order from the chat: the AI calls the customer to confirm it."""
    number = assistant.normalize_phone(order.get("phone", ""))
    if not number or not phone.is_configured():
        return
    try:
        phone.start_outbound_call(number, message.chat.id, message.from_user.username, order=order)
    except Exception as e:
        print(f"Failed to start confirmation call to chat {message.chat.id}: {e}")
        return
    bot.send_message(message.chat.id, TEXTS["confirm_calling"])


@bot.message_handler(commands=['chat'])
def start_chat(message, opening=None):
    if not limits.start_conversation(message.from_user.id):
        bot.send_message(message.chat.id, TEXTS["limit_reached"])
        return
    history = [{"role": "user", "content": opening or TEXTS["call_start"]}]
    active_chats[message.chat.id] = history
    handle_turn(message, history)


@bot.callback_query_handler(func=lambda call: call.data.startswith("ask:"))
def ask_about_product(call):
    """"💬 Запитати" under a product card: starts the chat about this product."""
    bot.answer_callback_query(call.id)
    product = next((p for p in assistant.CATALOG if p["id"] == call.data[4:]), None)
    if not product or call.message.chat.id in active_chats:
        return
    call.message.from_user = call.from_user  # the chat belongs to whoever tapped the button
    start_chat(call.message, f"Доброго дня! Мене цікавить: {product['title']}.")


@bot.message_handler(commands=['end'])
def end_chat(message):
    if message.chat.id not in active_chats:
        bot.send_message(message.chat.id, TEXTS["no_active_call"])
        return
    finish_chat(message)
    bot.send_message(message.chat.id, TEXTS["call_ended"])


@bot.message_handler(func=lambda m: m.chat.id in active_chats, content_types=['text'])
def continue_chat(message):
    history = active_chats[message.chat.id]
    user_messages = sum(m["role"] == "user" for m in history)  # the opening + this one - 1
    if not limits.add_message(message.from_user.id, user_messages):
        history.append({"role": "user", "content": message.text})
        bot.send_message(message.chat.id, TEXTS["limit_reached"])
        finish_chat(message)
        return
    history.append({"role": "user", "content": message.text})
    handle_turn(message, history)


def handle_turn(message, history):
    bot.send_chat_action(message.chat.id, "typing")
    try:
        reply, finished = assistant.chat_reply(history)
    except anthropic.APIStatusError as e:
        print(f"Claude API error {e.status_code}: {e.message}")
        history.pop()
        bot.send_message(message.chat.id, TEXTS["error"])
        return
    except anthropic.APIConnectionError:
        history.pop()
        bot.send_message(message.chat.id, TEXTS["connection_error"])
        return

    if not reply:
        history.pop()
        bot.send_message(message.chat.id, TEXTS["refusal"])
        return

    history.append({"role": "assistant", "content": reply})
    bot.send_message(message.chat.id, reply)

    if finished:
        finish_chat(message)
        bot.send_message(message.chat.id, TEXTS["call_saved"])


@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.send_message(message.chat.id, TEXTS["unknown"])

# ---------------------------------------------------------------------------
# Order status: a manager changes "Статус обробки" in Google Sheets -> the customer is told
# ---------------------------------------------------------------------------


def check_order_statuses():
    statuses = {s.lower(): s for s in assistant.ORDER_STATUSES}
    for order_id, sheet_status in sheets.read_order_statuses():
        order = assistant.get_order(int(order_id))
        if not order or not sheet_status:
            continue
        status = statuses.get(sheet_status.lower(), sheet_status)
        if status == (order["order_status"] or assistant.ORDER_STATUSES[0]):
            continue
        assistant.set_order_status(order["id"], status)
        print(f"Order №{order['id']}: {order['order_status']} -> {status}")
        template = assistant.ORDER_STATUS_MESSAGES.get(status)
        if template and order["chat_id"]:
            text = template.format_map(defaultdict(str, {
                **order, "name": order["customer_name"] or "Шановний клієнте"}))
            try:
                bot.send_message(order["chat_id"], text)
            except Exception as e:
                print(f"Could not tell chat {order['chat_id']} about order №{order['id']}: {e}")


def watch_order_statuses():
    while True:
        time.sleep(STATUS_CHECK_SECONDS)
        try:
            check_order_statuses()
        except Exception as e:
            print(f"Could not check order statuses in Google Sheets: {e}")


if __name__ == "__main__":
    assistant.init_db()
    limits.init_db()
    if sheets.is_configured():
        threading.Thread(target=watch_order_statuses, daemon=True).start()
        print(f"Google Sheets: on, order statuses checked every {STATUS_CHECK_SECONDS} s")
    else:
        print("Google Sheets: off")
    if phone.PUBLIC_URL:
        webcall.preload_whisper()
        threading.Thread(target=phone.run_server, daemon=True).start()
        print(f"Call server: {phone.PUBLIC_URL} -> localhost:{phone.PORT}")
        print(f"Telegram calls (/call): {phone.PUBLIC_URL}/webcall")
        print("Twilio phone calls: " + ("on" if phone.is_configured() else "off"))
    else:
        print("Calls are off: add PUBLIC_URL (ngrok address) to config.py")
    print(f"Berry Farm bot is running... ({len(assistant.CATALOG)} products, "
          f"{len(assistant.FAQ)} FAQ, {len(assistant.SCRIPT)} script steps)")
    bot.infinity_polling()
