from dotenv import load_dotenv; load_dotenv()
import sys
from datetime import datetime
from meritz_wrapper import place_overseas_order, get_last_close_yfinance, KST
kind = sys.argv[1]                                   # LOC | MOC
last = get_last_close_yfinance("TQQQ")[0]
price = round(last * 0.5, 2) if kind == "LOC" else None   # LOC는 체결 안 되게 낮은 가격
print(place_overseas_order("TQQQ", "BUY", 1, price, kind=kind))
print("submitted_at:", datetime.now(KST).isoformat(timespec="seconds"))