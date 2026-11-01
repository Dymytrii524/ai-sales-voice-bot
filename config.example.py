# Copy this file to config.py and fill in your values.
# config.py is in .gitignore - never commit real keys.

# --- Required -------------------------------------------------------------

# Telegram bot token from @BotFather
API_TOKEN = "123456789:your-telegram-bot-token"

# Anthropic API key from https://console.anthropic.com
ANTHROPIC_API_KEY = "sk-ant-..."

# --- Voice calls in Telegram (/call) ---------------------------------------

# Public HTTPS address of this computer/server, e.g. from `ngrok http 8000`
PUBLIC_URL = "https://your-subdomain.ngrok-free.app"
# PHONE_SERVER_PORT = 8000

# --- Google Sheets (orders table + order status notifications) -------------

# The table must be shared (Editor) with the service account's e-mail
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/.../edit"
# GOOGLE_CREDENTIALS = "google_credentials.json"   # service account key file

# --- Limits per Telegram user (optional, defaults shown) -------------------

# ADMIN_IDS = [123456789]                  # your Telegram id(s): no limits
# LIMIT_CONVERSATIONS_PER_DAY = 5
# LIMIT_MESSAGES_PER_DAY = 80
# LIMIT_MESSAGES_PER_CONVERSATION = 40
# LIMIT_CALL_MINUTES_PER_DAY = 20
# LIMIT_ALL_USERS_MESSAGES_PER_DAY = 3000  # whole bot's daily budget

# --- Real phone calls via Twilio (optional, paid) --------------------------

# TWILIO_ACCOUNT_SID = "AC..."
# TWILIO_AUTH_TOKEN = "..."
# TWILIO_PHONE_NUMBER = "+1..."
