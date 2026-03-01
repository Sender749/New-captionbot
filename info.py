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


ADMIN = int(getenv("ADMIN", "6541030917"))
SILICON_PIC = os.environ.get("SILICON_PIC", "https://telegra.ph/file/21a8e96b45cd6ac4d3da6.jpg")
API_ID = int(getenv("API_ID", "25208597"))
API_HASH = str(getenv("API_HASH", "e99c3c5693d6d23a143b6ce760b7a6de"))
BOT_TOKEN = str(getenv("BOT_TOKEN", ""))
BOT_USERNAME = str(getenv("BOT_USERNAME", "Navex_AutoCaptionbot"))
FORCE_SUB = os.environ.get("FORCE_SUB", "") 
MONGO_DB = str(getenv("MONGO_DB", "mongodb+srv://gd3251791_db_user:LiZ92DMTEM4iqD8H@cluster0.diqbn3b.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0",))
LOG_CH = os.environ.get("LOG_CH", "-1002904285991") 
FF_CH = int(os.environ.get("FF_CH", "-1003073036876"))
CP_CH = int(os.environ.get("CP_CH", "-1002920306518"))
DEF_CAP = str(
    getenv(
        "DEF_CAP",
        "<b>File Name:- `{file_name}`\n\n{file_size}</b>",
    )
)
