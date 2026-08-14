#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Показывает chat_id чатов, в которые писали вашему боту.

1) Добавьте бота в группу (администратором) и отправьте в неё любое сообщение.
2) Запустите: python3 get_chat_id.py <ТОКЕН_БОТА>
"""
import json
import sys
import urllib.error
import urllib.request


def main():
    token = sys.argv[1] if len(sys.argv) > 1 else input("Токен бота: ").strip()
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
        sys.exit(f"Ошибка Telegram API {err.code}: {body}")
    except urllib.error.URLError as err:
        sys.exit(f"Не удалось связаться с Telegram: {err}")

    chats = {}
    for update in data.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            event = update.get(key)
            if event:
                chat = event["chat"]
                title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                chats[chat["id"]] = title
    if not chats:
        print("Обновлений нет. Напишите любое сообщение в группу с ботом и запустите снова.")
        return
    print("chat_id — название")
    for chat_id, title in chats.items():
        print(f"{chat_id} — {title}")


if __name__ == "__main__":
    main()
