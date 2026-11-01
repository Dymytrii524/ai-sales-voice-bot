"""Shared logic for the Telegram chat and the phone calls:
CSV data, prompts, Claude requests and the calls database."""
import csv
import json
import os
import sqlite3
from datetime import datetime

import anthropic

import config
import sheets

client = anthropic.Anthropic(api_key=getattr(config, "ANTHROPIC_API_KEY", None) or None)
async_client = anthropic.AsyncAnthropic(api_key=getattr(config, "ANTHROPIC_API_KEY", None) or None)

MODEL = "claude-haiku-5-5"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(BASE_DIR, "users.db")
CALLS_CSV = os.path.join(DATA_DIR, "calls.csv")
END_MARKER = "[END_CALL]"

# ---------------------------------------------------------------------------
# CSV data (edit the files in data/ and restart the bot)
# ---------------------------------------------------------------------------


def read_csv(name):
    """data/<name>, or the demo sample in data/sample/ when the full file isn't there
    (the full catalog and FAQ are built by import_data.py and aren't published)."""
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        path = os.path.join(DATA_DIR, "sample", name)
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


TEXTS = {r["key"]: r["text"] for r in read_csv("texts.csv")}
INFO = {r["key"]: r["value"] for r in read_csv("info.csv")}
SCRIPT = read_csv("script.csv")
CONFIRM_SCRIPT = read_csv("confirm_script.csv")
CATALOG = read_csv("catalog.csv")
FAQ = read_csv("faq.csv")
ORDER_STATUS_MESSAGES = {r["status"].strip(): r["message"].strip() for r in read_csv("order_statuses.csv")}
ORDER_STATUSES = list(ORDER_STATUS_MESSAGES)  # the first one is given to every new order
sheets.ORDER_STATUSES = ORDER_STATUSES

CATEGORIES = sorted({p["category"] for p in CATALOG})


def format_price(product):
    price = f"{float(product['price_uah']):g} грн"
    if product["old_price_uah"]:
        price += f" (було {float(product['old_price_uah']):g})"
    return price


def in_stock(product):
    return int(product["in_stock"] or 0) > 0


def category_text(category):
    lines = [f"🌱 {category}:\n"]
    for p in CATALOG:
        if p["category"] == category:
            mark = "✅" if in_stock(p) else "⏳ немає в наявності"
            hit = " 🔥" if p["is_hit"] == "1" else ""
            lines.append(f"• {p['title']}{hit}\n  {format_price(p)} {mark}")
    lines.append(f"\n{TEXTS['catalog_footer']}")
    return "\n".join(lines)


def about_text():
    return (
        f"🌱 {INFO['company_name']}\n\n"
        f"{INFO['about']}\n\n"
        f"🚚 {INFO['delivery']}\n"
        f"🛒 Мінімальна сума замовлення: {INFO['min_order_uah']} грн\n"
        f"💰 Знижки за кількістю: {INFO['discount_by_quantity']}\n"
        f"💰 Знижки від суми: {INFO['discount_by_order_sum']}\n"
        f"✉️ {INFO['email']}\n"
        f"🌐 {INFO['website']}\n\n"
        f"{TEXTS['about_bot']}"
    )

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

CHANNEL_RULES = {
    "chat": "Ти спілкуєшся з клієнтом у Telegram-чаті. Пиши коротко і дружньо - 1-3 речення за раз, по одному питанню.",
    "phone": (
        "Це ТЕЛЕФОННА розмова: твої відповіді озвучує синтезатор мовлення, а слова клієнта "
        "розпізнаються автоматично (можливі помилки розпізнавання - якщо щось незрозуміло, перепитай). "
        "Говори дуже коротко - 1-2 речення, по одному питанню. Без емодзі, списків, дужок і markdown. "
        "Номери телефонів повторюй для підтвердження групами цифр."
    ),
}


MODE_RULES = {
    "sales": "",
    "confirm": f"""
# Це дзвінок для ПІДТВЕРДЖЕННЯ замовлення
- Дані замовлення є в першому повідомленні розмови. Клієнт їх не бачить - зачитуй сам.
- Не продавай і не консультуй довго: мета - перевірити, що замовлення справжнє, і звірити дані.
- Якщо клієнт каже, що нічого не замовляв, не розуміє, про що мова, або це чужий номер - вибачся, попрощайся і заверши розмову ({END_MARKER}).
- Якщо клієнту незручно говорити - запитай, коли передзвонити, попрощайся і заверши розмову.
- Якщо клієнт хоче скасувати замовлення - уточни причину, прийми скасування, попрощайся і заверши розмову.
- Якщо чуєш автовідповідач або голосову пошту - одразу заверши розмову.
""",
}


def build_system_prompt(channel, mode="sales"):
    script = "\n".join(
        f"{s['step']}. {s['stage']}: {s['goal']}\n   Приклад: «{s['example_phrase']}»"
        for s in (CONFIRM_SCRIPT if mode == "confirm" else SCRIPT)
    )
    catalog = "\n".join(
        f"- [{p['category']}] {p['title']} | {format_price(p)} | "
        f"{'в наявності' if in_stock(p) else 'немає в наявності'}"
        + (f" | {p['features']}" if p["features"] else "")
        for p in CATALOG
    )
    faq = "\n".join(f"П: {q['question']}\nВ: {q['answer']}" for q in FAQ)
    info = "\n".join(f"- {k}: {v}" for k, v in INFO.items())

    return f"""Ти - ввічливий менеджер з продажу розплідника саджанців «Беррі Фарм Юкрейн» (berry-farm.com).
{CHANNEL_RULES[channel]} Говори українською (або мовою клієнта).

# Сценарій розмови (дотримуйся по порядку, але природно)
{script}

# Правила
- Ціни, наявність і характеристики бери ЛИШЕ з каталогу нижче. Якщо чогось немає - скажи, що менеджер уточнить під час підтвердження.
- Товари «немає в наявності» можна запропонувати під замовлення, але попередь про це.
- На питання про догляд і вирощування відповідай за базою FAQ.
- Якщо клієнт питає щось не по темі, ввічливо поверни розмову до підбору саджанців.
- Коли розмова завершена (клієнт підтвердив замовлення або сказав, що не хоче замовляти), скажи прощальну фразу і в самому кінці додай маркер {END_MARKER}.
{MODE_RULES[mode]}
# Інформація про компанію
{info}

# Каталог
{catalog}

# FAQ
{faq}
"""


SYSTEM_PROMPTS = {channel: build_system_prompt(channel) for channel in CHANNEL_RULES}
CONFIRM_PROMPT = build_system_prompt("phone", "confirm")


def format_order(data):
    """Order data (from extract_data) as text for the confirmation call."""
    labels = {"customer_name": "Ім'я", "phone": "Телефон", "products": "Товари",
              "quantity": "Кількість", "city": "Місто", "delivery": "Доставка", "notes": "Примітки"}
    return "\n".join(f"{label}: {data[key]}" for key, label in labels.items() if data.get(key))


def normalize_phone(phone):
    """'097 123-45-67' / '380971234567' -> '+380971234567'; '' if it doesn't look like a UA number."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    return "+" + digits if len(digits) == 12 and digits.startswith("380") else ""

EXTRACT_FIELDS = ["customer_name", "phone", "products", "quantity",
                  "city", "delivery", "notes", "status", "verdict", "verdict_reason"]
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        **{f: {"type": "string"} for f in EXTRACT_FIELDS},
        "status": {"type": "string", "enum": ["order", "interested", "declined"]},
        "verdict": {"type": "string", "enum": ["genuine", "fake", "unclear"]},
    },
    "required": EXTRACT_FIELDS,
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def ask_claude(history, system, output_config):
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=system,
        messages=history,
        output_config=output_config,
        cache_control={"type": "ephemeral"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        return None
    return "".join(b.text for b in response.content if b.type == "text").strip()


def chat_reply(history, channel="chat"):
    """Returns (reply_text, finished) or (None, False) on refusal. Raises anthropic errors."""
    reply = ask_claude(history, SYSTEM_PROMPTS[channel], {"effort": "low"})
    if not reply:
        return None, False
    finished = END_MARKER in reply
    return reply.replace(END_MARKER, "").strip(), finished


def extract_data(history):
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    text = ask_claude(
        [{"role": "user", "content": transcript}],
        "Витягни дані замовлення з розмови менеджера з клієнтом. "
        "products - назви саджанців з каталогу, quantity - кількість по кожному. "
        "Якщо якогось поля немає в розмові - залиш порожній рядок. "
        "verdict - чи замовлення справжнє: genuine - клієнт свідомо підтвердив замовлення і свої дані; "
        "fake - клієнт не робив замовлення, не розуміє про що мова, назвав чужі дані, жартує або це помилковий номер; "
        "unclear - розмова обірвалась, автовідповідач або недостатньо даних. "
        "verdict_reason - одне коротке речення, чому такий висновок.",
        {"effort": "low", "format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
    )
    return json.loads(text) if text else None

# ---------------------------------------------------------------------------
# Database (SQLite + copy in data/calls.csv)
# ---------------------------------------------------------------------------

CALL_COLUMNS = ["channel", "chat_id", "username", "caller_phone", *EXTRACT_FIELDS,
                "transcript", "created_at"]


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calls(
                id integer primary key autoincrement,
                chat_id integer,
                username text,
                customer_name text,
                phone text,
                products text,
                quantity text,
                city text,
                delivery text,
                notes text,
                status text,
                transcript text,
                created_at text
            );
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
        for column in ("channel", "caller_phone", "verdict", "verdict_reason", "order_status"):
            if column not in existing:
                conn.execute(f"ALTER TABLE calls ADD COLUMN {column} text")


def save_call(data, history, channel, chat_id=None, username="", caller_phone=""):
    row = {
        "channel": channel,
        "chat_id": chat_id,
        "username": username or "",
        "caller_phone": caller_phone,
        **{f: data[f] for f in EXTRACT_FIELDS},
        "transcript": "\n".join(
            f"{'Клієнт' if m['role'] == 'user' else 'Менеджер'}: {m['content']}"
            for m in history[1:]  # skip the automatic opening message
        ),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    with sqlite3.connect(DB_PATH) as conn:
        call_id = conn.execute(
            f"INSERT INTO calls ({', '.join(CALL_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in CALL_COLUMNS)})",
            [row[c] for c in CALL_COLUMNS],
        ).lastrowid
        if data["status"] == "order":
            conn.execute("UPDATE calls SET order_status = ? WHERE id = ?", (ORDER_STATUSES[0], call_id))

    new_file = not os.path.exists(CALLS_CSV)
    with open(CALLS_CSV, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CALL_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)

    sheets.add_call_in_background({**row, "id": call_id, "order_status": ORDER_STATUSES[0]})


def get_order(call_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    return dict(row) if row else None


def set_order_status(call_id, status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE calls SET order_status = ? WHERE id = ?", (status, call_id))


def finish_conversation(history, channel, **who):
    """Extracts the order data from a finished conversation and saves it.
    Returns the saved data, or None if nothing was saved."""
    if not history or len(history) < 3:
        return None
    try:
        data = extract_data(history)
        if data:
            save_call(data, history, channel, **who)
        return data
    except Exception as e:
        print(f"Failed to save {channel} conversation {who}: {e}")
        return None
