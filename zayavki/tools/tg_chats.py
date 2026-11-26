"""Узнать id чата для оповещений о бронях.

1) Добавьте бота в чат и напишите в чате: /start@имя_бота
2) python tools/tg_chats.py — покажет чаты, где бот видел сообщения (токен не выводится).
С ключом --test <id> — отправит пробное сообщение в этот чат.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

token = os.environ.get("ZAYAVKI_TG_BOT_TOKEN", "")
if not token:
    sys.exit("Нет ZAYAVKI_TG_BOT_TOKEN в .env")
call = lambda m, **p: json.loads(urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{m}", data=urllib.parse.urlencode(p).encode(), timeout=15).read())  # noqa: E731
me = call("getMe")["result"]
print(f"Бот: @{me['username']}")
if "--test" in sys.argv:
    chat = sys.argv[sys.argv.index("--test") + 1]
    call("sendMessage", chat_id=chat, text="Проверка: сюда будут приходить брони из панели заявок ✅")
    sys.exit("Отправлено")
chats = {}
for u in call("getUpdates")["result"]:
    for k in ("message", "my_chat_member", "channel_post"):
        ch = (u.get(k) or {}).get("chat")
        if ch:
            chats[ch["id"]] = ch.get("title") or ch.get("username") or ch.get("first_name")
print("\n".join(f"{i}  {t}" for i, t in chats.items()) or "Чатов нет: добавьте бота в чат и напишите там /start@" + me["username"])
