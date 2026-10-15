import os

import requests
from dotenv import load_dotenv

load_dotenv()


def send_telegram(msg: str) -> bool:
    """텔레그램 전송. 성공 여부를 반환한다 (parse_mode 없음: '_' 등으로 400 나는 문제 방지)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[TELEGRAM MOCK]", msg[:500])
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for i in range(0, len(msg), 4000):
        try:
            r = requests.post(url, json={"chat_id": chat, "text": msg[i:i + 4000]}, timeout=10)
            if not r.ok:
                print("telegram fail:", r.status_code, r.text[:200])
                ok = False
        except Exception as e:
            print("telegram fail:", e)
            ok = False
    return ok
