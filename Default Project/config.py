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
