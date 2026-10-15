"""수동 실행용 CLI (데몬과 동일한 로직 사용)

  python main.py --morning [--live] [TQQQ SOXL]    장중 주문 접수
  python main.py --aftermarket [--live] [TQQQ]      체결 정산
  python main.py --check                            실주문 전 API 점검(주문 없음)
기본은 DRY-RUN, 실주문/상태저장은 --live 를 줘야 한다.
"""
import sys
from datetime import datetime

from bot_daemon import ET, PORTFOLIO, morning_for_symbol, reconcile_for_symbol
from telegram_bot import send_telegram


def preflight():
    """실주문 전 점검: 토큰/거래소 매핑/일봉/잔고/주문가능금액 (주문은 내지 않음)"""
    import meritz_wrapper as m
    ok = True
    print("MAC:", m.MAC_ADDRESS, "| 실시간시세:", m.QUOTE_REALTIME)
    print("토큰:", "OK" if m.get_meritz_token() else "FAIL")
    for sym in PORTFOLIO:
        try:
            v, c = m.exch_for(sym)
            last, last5 = m.get_last_close_yfinance(sym)
            pos = m.get_position(sym)
            print(f"[{sym}] 거래소 {v}/{c} | 직전종가 {last} | 5일종가 {last5 or '부족'} | 보유 {pos['qty']:g}주")
        except Exception as e:
            ok = False
            print(f"[{sym}] FAIL: {e}")
    try:
        print("USD 주문가능금액:", m.get_usd_orderable())
    except Exception as e:
        ok = False
        print("주문가능금액 FAIL:", e)
    print("\nPREFLIGHT", "PASS" if ok else "FAIL")

if __name__ == "__main__":
    args = sys.argv[1:]
    live = "--live" in args
    syms = [a.upper() for a in args if not a.startswith("--")] or list(PORTFOLIO)
    today = datetime.now(ET).date().isoformat()
    if "--check" in args:
        preflight()
    elif "--morning" in args:
        for s in syms:
            try:
                morning_for_symbol(s, not live, today)
            except Exception as e:
                send_telegram(f"❌ [{s}] 주문 오류: {e}")
    elif "--aftermarket" in args:
        for s in syms:
            try:
                reconcile_for_symbol(s, not live)
            except Exception as e:
                send_telegram(f"❌ [{s}] 정산 오류: {e}")
    else:
        print(__doc__)
