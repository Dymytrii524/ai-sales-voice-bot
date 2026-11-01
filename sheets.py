"""Google Sheets: every saved conversation is added as a row.

  Sheet "Замовлення" - only conversations that ended with an order
                       (+ a "Статус обробки" column for managers to fill in)
  Sheet "Дзвінки"    - every conversation: chats, Telegram calls, phone calls

Setup: config.py -> GOOGLE_SHEET_URL, and the service account key saved as
google_credentials.json next to this file (see README steps in the chat).

Run `python sheets.py` once to check the connection and copy all
conversations already saved in users.db into the table.
"""
import os
import sqlite3
import threading

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHEET_URL = getattr(config, "GOOGLE_SHEET_URL", "")
CREDENTIALS = os.path.join(BASE_DIR, getattr(config, "GOOGLE_CREDENTIALS", "google_credentials.json"))

CHANNELS = {
    "telegram": "Чат Telegram",
    "webcall": "Дзвінок у Telegram",
    "phone": "Телефон (Twilio)",
    "phone_confirm": "Підтвердження (Twilio)",
}
STATUSES = {"order": "Замовлення", "interested": "Цікавився", "declined": "Відмова"}
VERDICTS = {"genuine": "Справжнє", "fake": "Фейк", "unclear": "Неясно"}

STATUS_COLUMN = "Статус обробки"
ORDER_STATUSES = []  # filled by main.py from data/order_statuses.csv - the dropdown in STATUS_COLUMN

# sheet title -> list of (column header, function(row) -> cell value)
# Columns are matched by header name, so they can be reordered in the table.
SHEETS = {
    "Замовлення": [
        ("№", lambda r: r.get("id", "")),
        ("Дата", lambda r: r["created_at"].replace("T", " ")[:16]),
        ("Ім'я", lambda r: r["customer_name"]),
        ("Телефон", lambda r: r["phone"] or r["caller_phone"]),
        ("Товари", lambda r: r["products"]),
        ("Кількість", lambda r: r["quantity"]),
        ("Місто", lambda r: r["city"]),
        ("Доставка", lambda r: r["delivery"]),
        ("Примітки", lambda r: r["notes"]),
        ("Перевірка ШІ", lambda r: VERDICTS.get(r.get("verdict"), r.get("verdict") or "")),
        ("Чому", lambda r: r.get("verdict_reason") or ""),
        ("Звідки", lambda r: CHANNELS.get(r["channel"], r["channel"])),
        ("Telegram", lambda r: f"@{r['username']}" if r["username"] else ""),
        (STATUS_COLUMN, lambda r: r.get("order_status") or "Нове"),
    ],
    "Дзвінки": [
        ("Дата", lambda r: r["created_at"].replace("T", " ")[:16]),
        ("Канал", lambda r: CHANNELS.get(r["channel"], r["channel"])),
        ("Результат", lambda r: STATUSES.get(r["status"], r["status"])),
        ("Ім'я", lambda r: r["customer_name"]),
        ("Телефон", lambda r: r["phone"] or r["caller_phone"]),
        ("Telegram", lambda r: f"@{r['username']}" if r["username"] else ""),
        ("Товари", lambda r: r["products"]),
        ("Кількість", lambda r: r["quantity"]),
        ("Місто", lambda r: r["city"]),
        ("Доставка", lambda r: r["delivery"]),
        ("Примітки", lambda r: r["notes"]),
        ("Перевірка ШІ", lambda r: VERDICTS.get(r.get("verdict"), r.get("verdict") or "")),
        ("Розмова", lambda r: r["transcript"]),
    ],
}

_lock = threading.Lock()
_spreadsheet = None
_headers = {}  # sheet title -> its header row


def is_configured():
    return bool(SHEET_URL) and os.path.exists(CREDENTIALS)


def spreadsheet():
    global _spreadsheet
    if _spreadsheet is None:
        import gspread
        _spreadsheet = gspread.service_account(filename=CREDENTIALS).open_by_url(SHEET_URL)
    return _spreadsheet


def worksheet(title):
    """Returns the sheet, creating it (or its missing columns) with a bold frozen header."""
    import gspread
    wanted = [name for name, _ in SHEETS[title]]
    try:
        ws = spreadsheet().worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet().add_worksheet(title, rows=1000, cols=len(wanted))

    if title not in _headers:
        header = ws.row_values(1)
        missing = [name for name in wanted if name not in header]
        if missing:  # new sheet, or a column added in a newer version of the bot
            header += missing
            if ws.col_count < len(header):
                ws.add_cols(len(header) - ws.col_count)
            ws.update([header], "A1")
            ws.format("1:1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
        if title == "Замовлення" and ORDER_STATUSES:
            add_status_dropdown(ws, header)
        _headers[title] = header
    return ws


def add_status_dropdown(ws, header):
    """Managers pick the order status from a list instead of typing it."""
    from gspread.utils import ValidationConditionType, rowcol_to_a1
    column = header.index(STATUS_COLUMN) + 1
    cells = f"{rowcol_to_a1(2, column)}:{rowcol_to_a1(ws.row_count, column)}"
    try:
        ws.add_validation(cells, ValidationConditionType.one_of_list, ORDER_STATUSES, showCustomUi=True)
    except Exception as e:
        print(f"Could not add the status dropdown: {e}")


def to_cells(title, row):
    values = {name: value(row) for name, value in SHEETS[title]}
    return [str(values.get(name, "") or "") for name in _headers[title]]


def add_call(row):
    """Adds one saved conversation (a row from assistant.save_call) to the table."""
    if not is_configured():
        return
    with _lock:
        calls_ws = worksheet("Дзвінки")
        calls_ws.append_row(to_cells("Дзвінки", row), value_input_option="RAW")
        if row["status"] == "order":
            orders_ws = worksheet("Замовлення")
            orders_ws.append_row(to_cells("Замовлення", row), value_input_option="RAW")


def read_order_statuses():
    """[(order №, status)] for every row of the "Замовлення" sheet."""
    with _lock:
        rows = worksheet("Замовлення").get_all_values()
    if not rows:
        return []
    header = rows[0]
    id_col, status_col = header.index("№"), header.index(STATUS_COLUMN)
    return [(r[id_col].strip(), r[status_col].strip())
            for r in rows[1:] if len(r) > max(id_col, status_col) and r[id_col].strip().isdigit()]


def add_call_in_background(row):
    """Google can be slow or unavailable - never let that block or break saving."""
    def work():
        try:
            add_call(row)
        except Exception as e:
            print(f"Could not add the conversation to Google Sheets: {e}")
    threading.Thread(target=work, daemon=True).start()


def sync_from_database():
    """Copies every conversation from users.db into empty sheets (first setup)."""
    with sqlite3.connect(os.path.join(BASE_DIR, "users.db")) as conn:
        conn.row_factory = sqlite3.Row
        rows = [{k: (v if v is not None else "") for k, v in dict(r).items()}
                for r in conn.execute("SELECT * FROM calls ORDER BY id")]
    for title in SHEETS:
        ws = worksheet(title)
        if len(ws.col_values(1)) > 1:
            print(f"'{title}': already has data, skipped")
            continue
        selected = rows if title == "Дзвінки" else [r for r in rows if r["status"] == "order"]
        if selected:
            ws.append_rows([to_cells(title, r) for r in selected], value_input_option="RAW")
        print(f"'{title}': copied {len(selected)} rows")


if __name__ == "__main__":
    if not SHEET_URL:
        print("Add GOOGLE_SHEET_URL to config.py")
    elif not os.path.exists(CREDENTIALS):
        print(f"Key file not found: {CREDENTIALS}")
    else:
        print("Connected to:", spreadsheet().title)
        sync_from_database()
        print("Done:", SHEET_URL)
