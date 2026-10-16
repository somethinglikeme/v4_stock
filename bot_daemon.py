"""V4 통합 자동매매 데몬

- 스케줄은 미국 동부시간(ET) 기준 -> 서머타임 자동 반영, 주말/휴장일 건너뜀
- 기본은 DRY-RUN. 실주문은 환경변수 LIVE_TRADING=1 일 때만.
- 중복 주문 방지: 주문 접수 의도를 먼저 state 에 저장(order_date/pending), 주문 직전 증권사 보유수량 대조
- 정산은 메리츠 주문체결 조회(기간)로 '실제 체결'만 반영. 대체 수단: manual_fills_<SYMBOL>.json
- 기록: orders.csv(주문) / daily.csv(정산) / cycles.csv(사이클) -> 대시보드용
- 알림: 시작 상태, 주문 접수, 장 마감 정산, 사이클 종료, 이상 알림, 하루 1회 하트비트
"""
import csv
import json
import os
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from config import PORTFOLIO, get_config
from storage import BASE_DIR, load_state, save_state
from v4_strategy import (calc_buy_orders, calc_sell_orders, calc_reverse_orders,
                         update_mode, record_orders, reconcile)
from meritz_wrapper import (get_last_close_yfinance, place_overseas_order, get_order_results,
                            get_position, get_usd_orderable, KST)
from telegram_bot import send_telegram

ET = ZoneInfo("America/New_York")
LIVE_TRADING = os.getenv("LIVE_TRADING", "0") == "1"


def _t(name, default):
    h, m = os.getenv(name, default).split(":")
    return dtime(int(h), int(m))


ORDER_TIME = _t("ORDER_TIME_ET", "09:35")          # 장 시작 직후 주문 접수
ORDER_CUTOFF = _t("ORDER_CUTOFF_ET", "15:30")       # 이 시각 이후엔 신규 주문 안 함
RECONCILE_TIME = _t("RECONCILE_TIME_ET", "16:20")   # 장 마감 후 정산
HEARTBEAT_TIME = _t("HEARTBEAT_TIME_KST", "21:00")  # 하루 1회 상태 알림 (KST)
MAX_ATTEMPTS = 6
RETRY_SEC = 600

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


# ------------------------------------------------------------------
# 주문 종류 / 표시
# ------------------------------------------------------------------
BUY_TYPES = ("first_buy", "star_buy", "avg_buy", "reverse_buy")
KIND_KOR = {"LOC": "LOC", "LIMIT": "지정가", "MOC": "MOC"}
TYPE_KOR = {"first_buy": "첫 매수", "star_buy": "별지점", "avg_buy": "평단", "reverse_buy": "리버스",
            "limit_sell": "3/4 목표가", "quarter_sell": "1/4 별지점", "reverse_sell": "리버스"}


def order_kind(o: dict) -> str:
    if o.get("moc"):
        return "MOC"
    if o["type"] == "limit_sell":
        return "LIMIT"
    return "LOC"


def side_of(o: dict) -> str:
    return "BUY" if o["type"] in BUY_TYPES else "SELL"


def kind_side_kor(o: dict) -> str:       # 예) "LOC 매수"
    return f"{KIND_KOR[order_kind(o)]} {'매수' if side_of(o) == 'BUY' else '매도'}"


FILL_ORDER = ["first_buy", "star_buy", "avg_buy", "reverse_buy", "quarter_sell", "limit_sell", "reverse_sell"]


def fill_label(t: str) -> str:           # 정산 메시지용 (체결된 type)
    if t == "first_buy":
        return "첫 매수"
    return f"{TYPE_KOR[t]} {'매수' if t in BUY_TYPES else '매도'}"


def money(x, nd=2) -> str:
    return f"${x:,.{nd}f}"


def pct(x) -> str:
    return "-" if x is None else f"{x:+.2f}%"


# ------------------------------------------------------------------
# 기록 (대시보드용 CSV)
# ------------------------------------------------------------------
ORDERS_FIELDS = ["date_kst", "order_date_et", "symbol", "type", "label", "kind", "side", "price", "qty",
                 "amount", "order_no", "status", "error", "T", "hold_qty", "avg_price"]
DAILY_FIELDS = ["filled_at_kst", "order_date_et", "symbol", "fill_types", "fill_qty", "buy_qty", "buy_price",
                "sell_qty", "sell_price", "eval_price", "total_qty", "avg_price", "splits", "T_before", "T",
                "invested_amt", "cash_left", "return_pct", "cum_return_pct", "broker_qty", "broker_match"]
CYCLES_FIELDS = ["symbol", "splits", "start_date", "end_date", "days", "total_buy_amt", "profit",
                 "return_pct", "cash_after"]


def append_csv(name: str, fields: list, row: dict):
    path = os.path.join(BASE_DIR, name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        if header != fields:   # 컬럼이 바뀌면 기존 파일은 보존하고 새로 시작
            os.replace(path, path + f".old_{datetime.now():%Y%m%d%H%M%S}")
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


# ------------------------------------------------------------------
# 상태 요약 (시작 메시지 / 하트비트 / main.py --status)
# ------------------------------------------------------------------
def next_order_kst(ordered_today: bool):
    now = datetime.now(ET)
    d = now.date()
    if ordered_today or now.time() >= ORDER_CUTOFF:
        d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    dt = datetime.combine(d, ORDER_TIME, tzinfo=ET).astimezone(KST)
    return "접수 시간대 진행 중 (곧 접수)" if dt <= datetime.now(KST) else dt.strftime("%m/%d %H:%M KST")


def build_status(title: str) -> str:
    now = datetime.now(ET)
    kst = lambda t: datetime.combine(now.date(), t, tzinfo=ET).astimezone(KST).strftime("%H:%M")
    mode = "LIVE(실주문)" if LIVE_TRADING else "DRY-RUN(주문/저장 안 함)"
    lines = [f"{title} [{mode}]",
             f"주문 {ORDER_TIME:%H:%M} ET ({kst(ORDER_TIME)} KST) / 정산 {RECONCILE_TIME:%H:%M} ET ({kst(RECONCILE_TIME)} KST)"]
    if _CAL is None:
        lines.append("⚠️ exchange_calendars 미설치: 휴장일을 거르지 못합니다 (pip install exchange_calendars)")
    try:
        lines.append(f"USD 주문가능: {money(get_usd_orderable())}")
    except Exception as e:
        lines.append(f"USD 주문가능: 조회 실패 ({str(e)[:60]})")
    ordered = True
    skip_check = os.getenv("V4_SKIP_BALANCE_CHECK", "0") == "1"
    for sym in PORTFOLIO:
        cfg = get_config(sym)
        try:
            st = load_state(sym, cfg["principal"])
        except Exception as e:
            lines += ["", f"[{sym}] ⚠️ 장부 읽기 실패: {e}"]
            continue
        ordered &= (st.order_date == now.date().isoformat())
        lines += ["", f"[{sym}] {cfg['splits']}분할 · 목표 +{cfg['target_pct']}%",
                  f"- 장부: T {st.T:.3f} · {st.hold_qty}주 · 평단 {money(st.avg_price)} · 남은금액 {money(st.cash)}"
                  f" · {'리버스' if st.is_reverse else '일반모드'}"]
        try:
            pos = get_position(sym)
            if int(round(pos["qty"])) == st.hold_qty:
                lines.append(f"- 증권사: {pos['qty']:g}주 ✅ 일치")
            else:
                note = "대조 생략 설정" if skip_check else "일치할 때까지 주문 중단"
                lines.append(f"- 증권사: {pos['qty']:g}주 ⚠️ 장부와 불일치 ({note})")
        except Exception as e:
            lines.append(f"- 증권사: 조회 실패 ({str(e)[:60]})")
        try:
            last, _ = get_last_close_yfinance(sym)
            vs = f" (평단 대비 {(last / st.avg_price - 1) * 100:+.1f}%)" if st.avg_price > 0 else ""
            lines.append(f"- 직전 종가 {money(last)}{vs}")
        except Exception as e:
            lines.append(f"- 직전 종가: 조회 실패 ({str(e)[:60]})")
        if st.pending:
            lines.append(f"- 미정산 주문 {len(st.pending)}건 (접수일 {st.order_date}) → 정산 대기")
        elif st.order_date == now.date().isoformat():
            lines.append("- 오늘 주문 접수 완료")
    lines += ["", f"다음 주문: {next_order_kst(ordered)}"]
    return "\n".join(lines)


# ==========================================
# 1. 장중 주문 접수
# ==========================================
def morning_for_symbol(symbol: str, dry_run: bool, today: str):
    cfg = get_config(symbol)
    splits, target, principal = cfg["splits"], cfg["target_pct"], cfg["principal"]
    compound = cfg.get("compound", False)
    tag = "[DRY] " if dry_run else ""
    state = load_state(symbol, principal)
    print(f"[{symbol}] 장중 주문 처리 시작 (날짜: {today})...")

    if state.order_date == today and not state.pending:
        print(f"[{symbol}] 이미 오늘({today}) 주문 처리가 완료되었습니다.")
        return
    if state.pending:
        print(f"[{symbol}] 미정산 주문이 존재합니다 (접수일: {state.order_date}).")
        if state.order_date != today:
            send_telegram(f"⛔ [{symbol}] {state.order_date} 주문이 미정산 상태라 신규 주문을 중단했습니다. 정산(체결 반영) 후 재개됩니다.")
        return

    if not dry_run and os.getenv("V4_SKIP_BALANCE_CHECK", "0") != "1":
        pos = get_position(symbol)     # 조회 실패 시 예외 -> 재시도 (확인 없이 주문하지 않음)
        if int(round(pos["qty"])) != state.hold_qty:
            send_telegram(f"⛔ [{symbol}] 증권사 보유 {pos['qty']:g}주 ≠ 장부 {state.hold_qty}주 - 중복 주문 방지를 위해 오늘 주문을 중단했습니다. "
                          f"장부(state_{symbol}.json)를 실제 체결에 맞춘 뒤 데몬을 재시작하세요.")
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
        orders = calc_reverse_orders(state, splits, last5_avg, last_close)
    else:
        orders = calc_buy_orders(state, target, splits, principal, last_close, compound) + \
                 calc_sell_orders(state, target, splits)
    orders = [o for o in orders if o["qty"] > 0]

    record_orders(state, orders, today)
    if not dry_run:
        save_state(symbol, state)  # 접수 '의도'를 먼저 저장 -> 중간에 죽어도 중복 주문 없음

    if not orders:
        send_telegram(f"{tag}[{symbol}] 오늘은 접수할 주문이 없습니다. (T {state.T:.3f} · 보유 {state.hold_qty}주)")
    for o in state.pending:
        side, kind = side_of(o), order_kind(o)
        if o.get("at_close"):
            price_txt = f"종가 체결 (LOC 한도 {money(o['price'])})"
            est_txt = f"≈{money(o['qty'] * last_close)} (직전 종가 기준)"
        elif kind == "MOC":
            price_txt, est_txt = "종가 (MOC)", f"≈{money(o['qty'] * last_close)}"
        else:
            price_txt, est_txt = money(o["price"]), money(o["qty"] * o["price"])
        msg = (f"📤 {tag}[{symbol} 주문 접수] {kind_side_kor(o)} ({TYPE_KOR[o['type']]})\n"
               f"- 종목: {symbol}\n- 주문종류: {kind_side_kor(o)}\n- 단가: {price_txt}\n"
               f"- 수량: {o['qty']}주\n- 주문금액: {est_txt}\n"
               f"- 현재: T {state.T:.3f} · 보유 {state.hold_qty}주 · 평단 {money(state.avg_price)}")
        row = {"date_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"), "order_date_et": today, "symbol": symbol,
               "type": o["type"], "label": kind_side_kor(o), "kind": kind, "side": side,
               "price": o.get("price") or "", "qty": o["qty"],
               "amount": round(o["qty"] * (last_close if o.get("at_close") else (o.get("price") or last_close)), 2),
               "T": round(state.T, 3), "hold_qty": state.hold_qty, "avg_price": round(state.avg_price, 2)}
        if dry_run:
            send_telegram(msg)
            continue
        o["submitted_at"] = datetime.now(KST).isoformat(timespec="seconds")
        r = place_overseas_order(symbol, side, o["qty"], o.get("price"), is_moc=(kind == "MOC"), kind=kind)
        o["placed"], o["order_no"] = bool(r.get("ok")), r.get("order_no")
        if r.get("ok"):
            send_telegram(msg)
            append_csv("orders.csv", ORDERS_FIELDS, dict(row, order_no=o["order_no"], status="접수"))
        else:
            err = r.get("error") or r.get("msg")
            send_telegram(f"❌ [{symbol}] 주문 실패 ({kind_side_kor(o)} {o['qty']}주): {err}")
            append_csv("orders.csv", ORDERS_FIELDS, dict(row, status="실패", error=err))
    if not dry_run:
        save_state(symbol, state)


# ==========================================
# 2. 장 마감 정산 (실제 체결 기준)
# ==========================================
def collect_fills(symbol: str, state) -> list:
    """정산용 체결 리스트 [{type, qty, price, filled_at?}]."""
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
            send_telegram(f"⚠️ [{symbol}] 주문 거부됨 ({kind_side_kor(o)} #{o['order_no']}): {r['reason']}")
        if r["qty"] > 0:
            out.append({"type": o["type"], "qty": r["qty"], "price": r["price"], "filled_at": r.get("filled_at", "")})
    return out


def eval_price(symbol: str, fills: list, state):
    """평가 가격 = 체결가. LOC 체결(종가)이 있으면 그 가격, 없으면 지정가 체결가. 체결이 없으면 당일 종가."""
    ok = [f for f in fills if f.get("qty", 0) > 0 and f.get("price", 0) > 0]
    pick = [f for f in ok if f["type"] != "limit_sell"] or ok
    if pick:
        return float(pick[-1]["price"]), "체결가"
    try:
        return float(get_last_close_yfinance(symbol, include_today=True)[0]), "종가"
    except Exception:
        return (state.recent_closes[-1] if state.recent_closes else 0.0), "직전 종가"


def reconcile_for_symbol(symbol: str, dry_run: bool):
    cfg = get_config(symbol)
    splits = cfg["splits"]
    state = load_state(symbol, cfg["principal"])
    if not state.pending:
        return
    fills = collect_fills(symbol, state)
    order_date, prev_avg = state.order_date, state.avg_price
    s = reconcile(state, fills, splits, trade_date=order_date)
    price, price_src = eval_price(symbol, fills, state)

    end = s["cycle_end"]
    invested = s["cycle_buy_amt"] if end else (state.cycle_buy_amt or state.hold_qty * state.avg_price)
    start_cash = s["cycle_start_cash"] if end else state.cycle_start_cash
    cur_ret = ((price / state.avg_price) - 1) * 100 if state.avg_price > 0 and price else None
    if end:
        cum_ret = s["cycle_return_pct"]
    elif invested > 0 and start_cash > 0 and price:
        cum_ret = (state.hold_qty * price + state.cash - start_cash) / invested * 100
    else:
        cum_ret = None

    broker_qty, match_txt = "", "대조 안 함"
    if not dry_run:
        try:
            pos = get_position(symbol)
            broker_qty = pos["qty"]
            if int(round(pos["qty"])) == state.hold_qty:
                match_txt = f"{pos['qty']:g}주 ✅ 일치"
            else:
                match_txt = f"{pos['qty']:g}주 ⚠️ 불일치 (장부 {state.hold_qty}주, 증권사 평단 {money(pos['avg'])}) - 확인 필요"
        except Exception as e:
            match_txt = f"조회 실패 ({str(e)[:60]})"

    filled_times = [f["filled_at"] for f in fills if f.get("filled_at")]
    if filled_times:
        when = f"{max(filled_times)} KST (미국장 {order_date})"
        when_csv = max(filled_times)
    else:
        when_csv = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        when = f"{when_csv} KST (정산 시각 · 미국장 {order_date})"

    if not s["filled_types"]:
        msg = (f"📥 [{symbol} 장 마감 정산]\n- 체결종목: {symbol}\n- 체결여부: X (체결 없음) · {price_src} {money(price)}\n"
               f"- 누적개수: {state.hold_qty}주 · 평단 {money(state.avg_price)} · T {state.T:.3f}\n"
               f"- 전체분할수: {splits}분할\n- 누적매수액: {money(invested)} · 남은금액: {money(state.cash)}\n"
               f"- 현재수익률: {pct(cur_ret)} · 누적수익률: {pct(cum_ret)}\n- 증권사 잔고: {match_txt}")
    else:
        labels = " · ".join(fill_label(t) for t in sorted(s["filled_types"], key=FILL_ORDER.index))
        px = []
        if s["buy_qty"]:
            px.append(f"매수 {money(s['buy_price'])}")
        if s["sell_qty"]:
            px.append(f"매도 {money(s['sell_price'])}")
        qty_txt = " / ".join(x for x in (f"매수 {s['buy_qty']}주" if s["buy_qty"] else "",
                                         f"매도 {s['sell_qty']}주" if s["sell_qty"] else "") if x)
        avg_txt = money(state.avg_price) if state.hold_qty else "-"
        msg = (f"📥 [{symbol} 장 마감 정산]\n- 체결종목: {symbol}\n- 체결여부: O ({labels})\n- 체결일시: {when}\n"
               f"- 체결가격: {' / '.join(px)}\n- 체결개수: {qty_txt}\n- 누적개수: {state.hold_qty}주\n"
               f"- 평균단가: {avg_txt}" + (f" (이전 {money(prev_avg)})" if prev_avg and state.hold_qty else "") + "\n"
               f"- 전체분할수: {splits}분할\n- 현재회차(T): {s['T_before']:.3f} → {state.T:.3f}\n"
               f"- 누적매수액: {money(invested)}\n- 남은금액: {money(state.cash)}\n"
               f"- 현재수익률: {pct(cur_ret)} (평단 대비, 평가가 {money(price)} {price_src} 기준)\n"
               f"- 누적수익률: {pct(cum_ret)} (사이클 누적)\n- 증권사 잔고: {match_txt}")
    if s["warnings"]:
        msg += "\n⚠️ " + " / ".join(s["warnings"])
    send_telegram(msg)

    if not dry_run:
        append_csv("daily.csv", DAILY_FIELDS, {
            "filled_at_kst": when_csv, "order_date_et": order_date, "symbol": symbol,
            "fill_types": "+".join(s["filled_types"]) or "none",
            "fill_qty": s["buy_qty"] + s["sell_qty"], "buy_qty": s["buy_qty"], "buy_price": round(s["buy_price"], 4),
            "sell_qty": s["sell_qty"], "sell_price": round(s["sell_price"], 4), "eval_price": round(price, 4),
            "total_qty": state.hold_qty, "avg_price": round(state.avg_price, 2), "splits": splits,
            "T_before": round(s["T_before"], 3), "T": round(state.T, 3), "invested_amt": round(invested, 2),
            "cash_left": round(state.cash, 2), "return_pct": "" if cur_ret is None else round(cur_ret, 2),
            "cum_return_pct": "" if cum_ret is None else round(cum_ret, 2), "broker_qty": broker_qty,
            "broker_match": match_txt.startswith(f"{broker_qty:g}주 ✅") if broker_qty != "" else ""})

    if end:
        start = s["cycle_start_date"]
        try:
            days = (datetime.fromisoformat(order_date) - datetime.fromisoformat(start)).days + 1
            period = f"{start} ~ {order_date} ({days}일)"
        except Exception:
            days, period = "", f"{start or '시작일 미상'} ~ {order_date}"
        send_telegram(f"🎉 [{symbol} 한 사이클 종료!]\n- 매매종목: {symbol}\n- 전체분할수: {splits}분할\n"
                      f"- 총누적매수액: {money(s['cycle_buy_amt'])}\n- 총누적수익률: {pct(s['cycle_return_pct'])}\n"
                      f"- 총누적수익액: {'+' if s['cycle_profit'] >= 0 else '-'}{money(abs(s['cycle_profit']))}\n"
                      f"- 기간: {period}\n- 정산 후 현금: {money(state.cash)} · 다음 사이클 첫 매수 대기")
        if not dry_run:
            append_csv("cycles.csv", CYCLES_FIELDS, {
                "symbol": symbol, "splits": splits, "start_date": start, "end_date": order_date, "days": days,
                "total_buy_amt": round(s["cycle_buy_amt"], 2), "profit": round(s["cycle_profit"], 2),
                "return_pct": round(s["cycle_return_pct"], 2), "cash_after": round(state.cash, 2)})
    if not dry_run:
        save_state(symbol, state)


# ==========================================
# 3. 스케줄러
# ==========================================
_attempts = {}   # key -> (count, last_ts)
_hb = {"date": None}


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
    now_kst = datetime.now(KST)
    if now_kst.time() >= HEARTBEAT_TIME and _hb["date"] != now_kst.date():
        _hb["date"] = now_kst.date()        # 실패해도 하루 1회만
        try:
            send_telegram(build_status("💓 V4 데몬 정상 가동"))
        except Exception as e:
            print("heartbeat error:", e)
    # 미정산 주문은 날짜가 지났거나 정산 시각이 지났으면 언제든 정산
    for sym in PORTFOLIO:
        st = load_state(sym, get_config(sym)["principal"])
        if st.pending and ((st.order_date and st.order_date < today) or now.time() >= RECONCILE_TIME):
            _run(("reconcile", sym, st.order_date, today), reconcile_for_symbol, sym, dry_run)
    if not is_trading_day(now.date()):
        return
    if ORDER_TIME <= now.time() < ORDER_CUTOFF:
        for sym in PORTFOLIO:
            _run(("morning", sym, today), morning_for_symbol, sym, dry_run, today)


def main_loop():
    dry = not LIVE_TRADING
    try:
        send_telegram(build_status("🤖 V4 데몬 시작"))
    except Exception as e:
        send_telegram(f"🤖 V4 데몬 시작 (상태 조회 실패: {e})")
    _hb["date"] = datetime.now(KST).date()     # 시작 메시지가 오늘의 하트비트를 대신함
    while True:
        try:
            tick(dry)
        except Exception as e:
            print("tick error:", e)
        time.sleep(30)


if __name__ == "__main__":
    main_loop()
