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
FF_CH = int(os.environ.get("FF_CH", "-1003073036876"))

# ── Admin global-forward destination ─────────────────────────────────────────
# Files forwarded via /channels → "File Forwarding" are sent here.
MANUAL_FF = int(os.environ.get("MANUAL_FF", "-1003979911149"))

# ── Member-channel forward (userbot) ─────────────────────────────────────────
# Pyrogram session string for a personal Telegram account (NOT the bot).
# Powers /member_forward, which pulls files out of channels where this
# account is a member/admin but the BOT ITSELF was never added — something
# the Bot API can never do on its own. Leave empty to disable the feature
# entirely; nothing else in the bot depends on it.
SESSION_STRING = str(getenv("SESSION_STRING", "BQHBa2AAB2Gkf7fVzKe7laAj3-sVJdoVgs7kdqElm_ivE4bUGIML4SNioZOtM_oBIk-Gal_oszjfAT7QIumIVsCMXVuyD0Gh29p1204DwCQ03-H28cieNGmi7-q75p0LETReT3xm54yhXKu1lfcpwu5eNMs9YeI9uPD2yeplb1ma3HyEFnTgJLSGXSR6Ww2EcNvVvum25FElPQlQ___oEdfTMygfTOmILhxkk3ehTTg1a0TrbfdGooam7-1eggRmFHw4kOQbjWRIvvVegOwlt-PZEfHYBviqr0KQftEAjSJ2pS6kvVM5qioOyGbSK8iIKraNBRp6SWv9JZpkDxyRagtMhQbtaAAAAAHGKGRSAA"))
