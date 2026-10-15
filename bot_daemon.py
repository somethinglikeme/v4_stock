"""V4 통합 자동매매 데몬 (패치본)

- 스케줄은 미국 동부시간(ET) 기준 -> 서머타임 자동 반영, 주말/휴장일 건너뜀
- 기본은 DRY-RUN. 실주문은 환경변수 LIVE_TRADING=1 일 때만.
- 중복 주문 방지: 주문 접수 의도를 먼저 state 에 저장(order_date/pending) -> 재시작해도 같은 날 재주문 없음
- 정산은 '실제 체결'로만 수행. 체결 조회 API 미구현 시 manual_fills_<SYMBOL>.json 사용
"""
import csv
import json
import os
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from config import PORTFOLIO, get_config
from storage import BASE_DIR, load_state, save_state
from v4_strategy import (calc_buy_orders, calc_sell_orders, calc_reverse_orders,
                         update_mode, record_orders, reconcile)
from meritz_wrapper import get_last_close_yfinance, place_overseas_order, get_order_results, get_position, KST
from telegram_bot import send_telegram

ET = ZoneInfo("America/New_York")
LIVE_TRADING = os.getenv("LIVE_TRADING", "0") == "1"


def _t(name, default):
    h, m = os.getenv(name, default).split(":")
    return dtime(int(h), int(m))


ORDER_TIME = _t("ORDER_TIME_ET", "09:35")        # 장 시작 직후 주문 접수
ORDER_CUTOFF = _t("ORDER_CUTOFF_ET", "15:30")     # 이 시각 이후엔 신규 주문 안 함
RECONCILE_TIME = _t("RECONCILE_TIME_ET", "16:20")  # 장 마감 후 정산
MAX_ATTEMPTS = 6
RETRY_SEC = 600
DASHBOARD_FILE = os.path.join(BASE_DIR, "dashboard_data.csv")

try:
    import exchange_calendars as _xcals
    _CAL = _xcals.get_calendar("XNYS")
except Exception:
    _CAL = None


def is_trading_day(d) -> bool:
    if d.weekday() >= 5:
        return False
    if _CAL is not None:
        try:
            return bool(_CAL.is_session(d.isoformat()))
        except Exception:
            return True
    return True


# 주문 type -> 증권사 주문 종류
def order_kind(o: dict) -> str:
    if o.get("moc") or o["type"] == "first_buy":
        return "MOC"
    if o["type"] == "limit_sell":
        return "LIMIT"
    return "LOC"


def save_to_dashboard(log_data: dict):
    exists = os.path.isfile(DASHBOARD_FILE)
    with open(DASHBOARD_FILE, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(log_data.keys()))
        if not exists:
            w.writeheader()
        w.writerow(log_data)


# ==========================================
# 1. 장중 주문 접수
# ==========================================
def morning_for_symbol(symbol: str, dry_run: bool, today: str):
    cfg = get_config(symbol)
    splits, target, principal = cfg["splits"], cfg["target_pct"], cfg["principal"]
    compound = cfg.get("compound", False)
    tag = "[DRY] " if dry_run else ""
    state = load_state(symbol, principal)

    if state.order_date == today and not state.pending:
        return
    if state.pending:
        if state.order_date != today:
            send_telegram(f"⛔ [{symbol}] {state.order_date} 주문이 미정산 상태라 신규 주문을 중단했습니다. 정산(체결 반영) 후 재개됩니다.")
        return

    last_close, last5 = get_last_close_yfinance(symbol)
    if last_close <= 0:
        raise RuntimeError(f"{symbol} 종가 이상: {last_close}")
    if state.recent_closes and abs(last_close / state.recent_closes[-1] - 1) > 0.35:
        raise RuntimeError(f"{symbol} 종가 급변 의심 ({state.recent_closes[-1]} -> {last_close}) - 수동 확인 필요")

    if len(last5) >= 5:
        state.recent_closes = list(last5)
    elif not state.recent_closes or state.recent_closes[-1] != last_close:
        state.recent_closes.append(last_close)
    state.recent_closes = state.recent_closes[-10:]
    last5_avg = sum(state.recent_closes[-5:]) / 5 if len(state.recent_closes) >= 5 else None

    for m in update_mode(state, splits, target, last_close):
        send_telegram(f"[{symbol}] {m}")

    if state.is_reverse:
        orders = calc_reverse_orders(state, splits, last5_avg)
    else:
        orders = calc_buy_orders(state, target, splits, principal, last_close, compound) + \
                 calc_sell_orders(state, target, splits)
    orders = [o for o in orders if o["qty"] > 0]

    record_orders(state, orders, today)
    if not dry_run:
        save_state(symbol, state)  # 접수 '의도'를 먼저 저장 -> 중간에 죽어도 중복 주문 없음

    if not orders:
        send_telegram(f"{tag}[{symbol}] 오늘은 접수할 주문이 없습니다.")
    for o in state.pending:
        side = "BUY" if o["type"] in ("first_buy", "star_buy", "avg_buy", "reverse_buy") else "SELL"
        kind = order_kind(o)
        price_txt = "MOC(종가 시장가)" if kind == "MOC" else f"${o['price']} ({kind})"
        msg = f"📤 {tag}[{symbol} 주문 접수]\n- 주문종류: {o['type']} ({side})\n- 단가: {price_txt}\n- 수량: {o['qty']}주"
        if dry_run:
            send_telegram(msg)
            continue
        o["submitted_at"] = datetime.now(KST).isoformat(timespec="seconds")
        r = place_overseas_order(symbol, side, o["qty"], o.get("price"), is_moc=(kind == "MOC"), kind=kind)
        o["placed"], o["order_no"] = bool(r.get("ok")), r.get("order_no")
        if r.get("ok"):
            send_telegram(msg)
        else:
            send_telegram(f"❌ [{symbol}] 주문 실패 ({o['type']} {o['qty']}주): {r.get('error') or r.get('msg')}")
    if not dry_run:
        save_state(symbol, state)


# ==========================================
# 2. 장 마감 정산 (실제 체결 기준)
# ==========================================
def collect_fills(symbol: str, state) -> list:
    """정산용 체결 리스트 [{type, qty, price}]."""
    manual = os.path.join(BASE_DIR, f"manual_fills_{symbol}.json")
    if os.path.exists(manual):
        with open(manual, "r", encoding="utf-8") as f:
            fills = json.load(f)
        os.replace(manual, manual + f".done_{datetime.now():%Y%m%d%H%M%S}")
        return fills
    placed = [o for o in state.pending if o.get("placed")]
    if not placed:
        return []
    if any(not o.get("order_no") or not o.get("submitted_at") for o in placed):
        raise RuntimeError("주문번호/접수시각이 없는 주문이 있어 자동 정산 불가 - manual_fills 로 처리하세요")
    results = get_order_results(placed)   # 못 찾으면 예외 (체결 없음으로 간주하지 않음)
    out = []
    for o in placed:
        r = results[str(o["order_no"])]
        if r["rejected"]:
            send_telegram(f"⚠️ [{symbol}] 주문 거부됨 ({o['type']} #{o['order_no']}): {r['reason']}")
        if r["qty"] > 0:
            out.append({"type": o["type"], "qty": r["qty"], "price": r["price"]})
    return out


def reconcile_for_symbol(symbol: str, dry_run: bool):
    cfg = get_config(symbol)
    splits = cfg["splits"]
    state = load_state(symbol, cfg["principal"])
    if not state.pending:
        return
    fills = collect_fills(symbol, state)
    pending_dates = state.order_date
    prev_hold = state.hold_qty
    s = reconcile(state, fills, splits)

    ref_price = s["sell_price"] or s["buy_price"] or (state.recent_closes[-1] if state.recent_closes else 0.0)
    ret = ((ref_price / state.avg_price) - 1) * 100 if state.avg_price > 0 and ref_price else 0.0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fill_qty = s["buy_qty"] + s["sell_qty"]
    log = {
        "date": now_str, "order_date": pending_dates, "symbol": symbol,
        "fill_type": "+".join(s["filled_types"]) or "none",
        "fill_qty": fill_qty,
        "fill_price": round(ref_price, 4) if fill_qty else 0.0,
        "total_qty": state.hold_qty, "avg_price": round(state.avg_price, 2), "T": round(state.T, 3),
        "invested_amt": round(state.hold_qty * state.avg_price, 2),
        "cash_left": round(state.cash, 2), "return_pct": round(ret, 2),
    }
    if not dry_run:
        save_to_dashboard(log)

    if not s["filled_types"]:
        msg = f"📥 [{symbol} 장 마감 정산]\n- 체결여부: X (체결 없음)\n- 현재회차(T): {state.T:.3f}\n- 누적개수: {state.hold_qty}주"
    else:
        msg = (f"📥 [{symbol} 장 마감 정산]\n- 체결여부: O ({', '.join(s['filled_types'])})\n"
               f"- 체결일시: {now_str}\n"
               f"- 매수체결: {s['buy_qty']}주 @ ${s['buy_price']:.2f}\n"
               f"- 매도체결: {s['sell_qty']}주 @ ${s['sell_price']:.2f}\n"
               f"- 누적개수: {state.hold_qty}주\n- 평균단가: ${log['avg_price']}\n"
               f"- 전체분할수: {splits}분할\n- 현재회차(T): {s['T_before']:.3f} -> {state.T:.3f}\n"
               f"- 누적매수액: ${log['invested_amt']}\n- 남은금액: ${log['cash_left']}\n"
               f"- 현재수익률: {log['return_pct']}%")
    if s["warnings"]:
        msg += "\n⚠️ " + " / ".join(s["warnings"])
    send_telegram(msg)

    if not dry_run:
        try:
            pos = get_position(symbol)
            if int(round(pos["qty"])) != state.hold_qty:
                send_telegram(f"⚠️ [{symbol}] 잔고 불일치: 증권사 {pos['qty']:g}주 vs 장부 {state.hold_qty}주 (평단 증권사 ${pos['avg']:.2f} / 장부 ${state.avg_price:.2f}) - 확인 필요")
        except Exception as e:
            send_telegram(f"⚠️ [{symbol}] 잔고 대조 실패: {e}")

    if s["cycle_end"]:
        send_telegram(f"🎉 [{symbol} 한 사이클 종료!]\n- 전체분할수: {splits}분할\n"
                      f"- 총 누적매수액: ${s['cycle_buy_amt']:.2f}\n"
                      f"- 총 누적수익률: {s['cycle_return_pct']:.2f}%\n"
                      f"- 총 누적수익액: ${s['cycle_profit']:.2f}\n- 정산 후 현금: ${state.cash:.2f}")
    if not dry_run:
        save_state(symbol, state)


# ==========================================
# 3. 스케줄러
# ==========================================
_attempts = {}   # key -> (count, last_ts)


def _run(key, fn, *args):
    cnt, last = _attempts.get(key, (0, 0.0))
    if cnt >= MAX_ATTEMPTS or time.time() - last < RETRY_SEC:
        return
    _attempts[key] = (cnt + 1, time.time())
    try:
        fn(*args)
        _attempts[key] = (MAX_ATTEMPTS, time.time())  # 성공 -> 오늘은 더 안 함
    except Exception as e:
        left = MAX_ATTEMPTS - cnt - 1
        send_telegram(f"❌ {key[0]} {key[1]} 오류: {e}\n(재시도 {left}회 남음)")


def tick(dry_run: bool):
    now = datetime.now(ET)
    today = now.date().isoformat()
    # 미정산 주문은 날짜가 지났거나 정산 시각이 지났으면 언제든 정산
    for sym in PORTFOLIO:
        st = load_state(sym, get_config(sym)["principal"])
        if st.pending and (st.order_date < today or now.time() >= RECONCILE_TIME):
            _run(("reconcile", sym, st.order_date, today), reconcile_for_symbol, sym, dry_run)
    if not is_trading_day(now.date()):
        return
    if ORDER_TIME <= now.time() < ORDER_CUTOFF:
        for sym in PORTFOLIO:
            _run(("morning", sym, today), morning_for_symbol, sym, dry_run, today)


def main_loop():
    dry = not LIVE_TRADING
    mode = "DRY-RUN(주문/저장 안 함)" if dry else "LIVE(실주문)"
    cal = "" if _CAL else "\n⚠️ exchange_calendars 미설치: 주말만 거르고 휴장일은 못 거릅니다 (pip install exchange_calendars)"
    send_telegram(f"🤖 V4 데몬 시작 [{mode}] 주문 {ORDER_TIME:%H:%M} ET / 정산 {RECONCILE_TIME:%H:%M} ET{cal}")
    while True:
        try:
            tick(dry)
        except Exception as e:
            print("tick error:", e)
        time.sleep(30)


if __name__ == "__main__":
    main_loop()
