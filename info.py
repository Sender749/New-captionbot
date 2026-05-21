from os import environ, getenv
import re
import os

id_pattern = re.compile(r"^.\d+$")


def is_enabled(value, default):
    if value.lower() in ["true", "yes", "1", "enable", "y"]:
        return True
    elif value.lower() in ["false", "no", "0", "disable", "n"]:
        return False
    else:
        return default


ADMIN = list(map(int, getenv("ADMIN", "6541030917 1052054451").split()))
API_ID = int(getenv("API_ID", "29453152"))
API_HASH = str(getenv("API_HASH", "2302adc174dbc954ae5081eda5131166"))
BOT_TOKEN = str(getenv("BOT_TOKEN", ""))
BOT_USERNAME = str(getenv("BOT_USERNAME", "Navex_AutoCaptionbot"))
FORCE_SUB = os.environ.get("FORCE_SUB", "") 
MONGO_DB = str(getenv("MONGO_DB", "mongodb+srv://gd3251791_db_user:LiZ92DMTEM4iqD8H@cluster0.diqbn3b.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0",))
LOG_CH = os.environ.get("LOG_CH", "-1002904285991") 
CP_CH = int(os.environ.get("CP_CH", "-1002920306518"))
FF_CH = os.environ.get("FF_CH", "-1003073036876")

# ── Admin global-forward destination ─────────────────────────────────────────
# Files forwarded via /channels → "File Forwarding" are sent here.
MANUAL_FF = int(os.environ.get("MANUAL_FF", "-1003741441979"))
