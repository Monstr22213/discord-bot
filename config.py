import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) if os.getenv("GUILD_ID") else None
VERIFY_ROLE_ID = int(os.getenv("VERIFY_ROLE_ID", "0")) if os.getenv("VERIFY_ROLE_ID") else None
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0")) if os.getenv("WELCOME_CHANNEL_ID") else None
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0")) if os.getenv("LOG_CHANNEL_ID") else None

SELF_ROLES_RAW = os.getenv("SELF_ROLES", "")
SELF_ROLES = [int(r.strip()) for r in SELF_ROLES_RAW.split(",") if r.strip().isdigit()]

# ============ AI (OpenRouter) ============
AI_ENABLED = os.getenv("AI_ENABLED", "true").lower() not in ("0", "false", "no", "off")
AI_API_KEY = os.getenv("OPENROUTER_API_KEY", "") or os.getenv("AI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1")
AI_MODEL = os.getenv("AI_MODEL", "meta-llama/llama-3.1-8b-instruct:free")
AI_SYSTEM_PROMPT = os.getenv("AI_SYSTEM_PROMPT", "Ты — дружелюбный бот на Discord сервере Black ICE Palace. Отвечай на русском, коротко (до 400 символов если не просят подробнее), с юмором но без токсичности. Помогай пользователям.")
# Имена через запятую, при упоминании которых бот ответит. Пример: "бот,мила,ice"
AI_TRIGGER_NAMES_RAW = os.getenv("AI_TRIGGER_NAMES", "")
AI_TRIGGER_NAMES = [s.strip().lower() for s in AI_TRIGGER_NAMES_RAW.split(",") if s.strip()]
AI_TRIGGER_ON_REPLY = os.getenv("AI_TRIGGER_ON_REPLY", "true").lower() not in ("0", "false", "no", "off")
AI_MAX_HISTORY = int(os.getenv("AI_MAX_HISTORY", "6"))  # сколько прошлых сообщений помнить на канал
AI_COOLDOWN = int(os.getenv("AI_COOLDOWN", "5"))  # сек между ответами одному юзеру
