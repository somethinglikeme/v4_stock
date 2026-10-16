"""V4 무한매수법 전략 엔진 (패치본)

원칙
- 주문 생성(calc_*)과 체결 정산(reconcile)을 분리한다.
- T / 수량 / 평단 / 현금은 '실제 체결 결과'로만 갱신한다.
- 모든 가격은 소수 2자리로 반올림, 수량은 정수, 주문금액은 현금을 넘지 않는다.
"""
import math
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

BUY_TYPES = {"first_buy", "star_buy", "avg_buy", "reverse_buy"}
SELL_TYPES = {"limit_sell", "quarter_sell", "reverse_sell"}


@dataclass
class V4State:
    T: float = 0.0
    cash: float = 20000
    hold_qty: int = 0
    avg_price: float = 0.0
    is_reverse: bool = False
    recent_closes: List[float] = field(default_factory=list)
    # ---- 패치로 추가된 필드 (기존 state_*.json 은 기본값으로 로드됨) ----
    reverse_day: int = 0          # 리버스 진입 후 정산 완료된 일수 (0 = 리버스 첫날)
    cycle_start_cash: float = 0.0  # 사이클 시작 시점 현금 (사이클 손익 계산용)
    cycle_buy_amt: float = 0.0     # 사이클 누적 매수액
    order_date: str = ""           # 마지막으로 주문을 접수한 날짜 (ET, YYYY-MM-DD)
    pending: List[dict] = field(default_factory=list)  # 접수됐지만 미정산인 주문


def _r2(x: float) -> float:
    return round(float(x) + 1e-12, 2)


# ------------------------------------------------------------------
# 수식
# ------------------------------------------------------------------
def calc_star_pct(target_pct: float, T: float, splits: int) -> float:
    return target_pct * (1 - 2 * T / splits) / 100


def calc_star_point(avg_price, target_pct, T, splits):
    if avg_price == 0:
        return 0
    return avg_price * (1 + calc_star_pct(target_pct, T, splits))


def calc_one_buy_amount(state: V4State, original_principal: float, splits: int,
                        compound: bool = False) -> float:
    """1회 매수금. 현금을 절대 넘지 않는다 (0 < splits-T < 1 구간 방어)."""
    if state.hold_qty == 0:
        base = state.cash if compound else min(state.cash, original_principal)
        return min(base / splits, state.cash)
    denom = splits - state.T
    if denom <= 0:
        return 0.0
    return min(state.cash / denom, state.cash)


# ------------------------------------------------------------------
# 주문 생성
# ------------------------------------------------------------------
def calc_buy_orders(state: V4State, target_pct: float, splits: int, principal: float,
                    last_close: float, compound: bool = False):
    if not last_close or last_close <= 0:
        return []
    one_amount = calc_one_buy_amount(state, principal, splits, compound)
    big_clamp = last_close * 1.15

    def clamp(p):
        return _r2(min(p, big_clamp))

    if state.hold_qty == 0:
        qty = math.floor(one_amount / last_close)
        # 메리츠 MOC 미지원 방어: 전일 종가의 +15% 가격 설정 (LOC 주문용)
        first_buy_price = _r2(last_close * 1.15)
        return [{"type": "first_buy", "price": first_buy_price, "qty": qty,
                 "amount": _r2(one_amount), "moc": False}]

    star_point = calc_star_point(state.avg_price, target_pct, state.T, splits)
    buy_point = star_point - 0.01
    if buy_point <= 0:
        return []
    star_price = clamp(buy_point)

    if state.T < splits / 2:
        half = one_amount * 0.5
        avg_price = clamp(state.avg_price)
        star_qty = math.floor(half / star_price)
        avg_qty = math.floor(half / avg_price) if avg_price > 0 else 0
        return [
            {"type": "star_buy", "price": star_price, "qty": star_qty, "amount": _r2(half)},
            {"type": "avg_buy", "price": avg_price, "qty": avg_qty, "amount": _r2(half)},
        ]
    qty = math.floor(one_amount / star_price)
    return [{"type": "star_buy", "price": star_price, "qty": qty, "amount": _r2(one_amount)}]


def calc_sell_orders(state: V4State, target_pct: float, splits: int):
    if state.hold_qty <= 0 or state.avg_price <= 0:
        return []
    limit_price = _r2(state.avg_price * (1 + target_pct / 100))
    star_point = _r2(calc_star_point(state.avg_price, target_pct, state.T, splits))
    hold = state.hold_qty
    q1 = math.floor(hold * 0.25)
    q2 = hold - q1
    return [
        {"type": "limit_sell", "price": limit_price, "qty": q2},
        {"type": "quarter_sell", "price": star_point, "qty": q1},
    ]


def calc_reverse_orders(state: V4State, splits: int, last_5_avg: Optional[float],
                        last_close: Optional[float] = None):
    """리버스: 첫날 종가 매도(MOC 대체: 낮은 가격 LOC) / 이후 5일평균 LOC 매도 + 현금 1/4 LOC 매수.

    메리츠는 해외주식 MOC 미지원 -> LOC 매도 하한가를 직전 종가의 50%로 둔다.
    LOC 는 한도가 아니라 종가로 체결되므로 사실상 '종가 매도'와 같다.
    """
    sell_qty = min(state.hold_qty, max(1, math.floor(state.hold_qty / (splits / 2)))) \
        if state.hold_qty > 0 else 0
    if state.reverse_day == 0 or not last_5_avg:
        if not last_close or last_close <= 0:
            raise ValueError("리버스 첫날 매도에는 직전 종가(last_close)가 필요합니다")
        return [{"type": "reverse_sell", "price": _r2(last_close * 0.5), "qty": sell_qty, "moc": False}]

    orders = [{"type": "reverse_sell", "price": _r2(last_5_avg), "qty": sell_qty}]
    buy_price = _r2(last_5_avg - 0.01)
    quarter_cash = state.cash / 4
    buy_qty = math.floor(quarter_cash / buy_price) if buy_price > 0 else 0
    if buy_qty >= 1:
        orders.append({"type": "reverse_buy", "price": buy_price, "qty": buy_qty,
                       "amount": _r2(quarter_cash)})
    return orders


# ------------------------------------------------------------------
# 모드 전환 (일반 <-> 리버스)
# ------------------------------------------------------------------
def update_mode(state: V4State, splits: int, target_pct: float, last_close: float) -> List[str]:
    msgs = []
    if state.is_reverse:
        if state.avg_price > 0 and last_close > state.avg_price * (1 - target_pct / 100):
            state.is_reverse = False
            state.reverse_day = 0
            # 탈출 직후 T>splits-1 이면 같은 날 재진입(핑퐁)하므로 splits-1 로 캡
            if state.T > splits - 1:
                state.T = float(splits - 1)
            msgs.append("🔄 리버스 종료 -> 일반모드 복귀")
    elif state.hold_qty > 0 and state.T > splits - 1:
        state.is_reverse = True
        state.reverse_day = 0
        msgs.append("⚠️ 리버스 모드 진입 (T > splits-1)")
    return msgs


# ------------------------------------------------------------------
# T 전이
# ------------------------------------------------------------------
def transition_T(T: float, is_reverse: bool, filled: set, splits: int) -> float:
    """filled: 실제 체결된 주문 type 집합. T는 주문 시점(정산 전) 값."""
    if is_reverse:
        if "reverse_sell" in filled:
            T = T * (1 - 2 / splits)
        if "reverse_buy" in filled:
            T = T + (splits - T) * 0.25
        return T

    if "first_buy" in filled:
        return 1.0

    if "limit_sell" in filled:
        base = T * 0.25
    elif "quarter_sell" in filled:
        base = T * 0.75
    else:
        base = T

    s, a = "star_buy" in filled, "avg_buy" in filled
    inc = 0.0
    if s or a:
        inc = 1.0 if ((s and a) or T >= splits / 2) else 0.5
    return base + inc


# ------------------------------------------------------------------
# 주문 기록 / 체결 정산
# ------------------------------------------------------------------
def record_orders(state: V4State, orders: list, order_date: str):
    """접수 의도를 기록한다. T/수량/현금은 건드리지 않는다."""
    state.order_date = order_date
    state.pending = [dict(o, placed=False, order_no=None) for o in orders if o.get("qty", 0) > 0]


def reconcile(state: V4State, fills: List[Dict[str, Any]], splits: int) -> Dict[str, Any]:
    """실제 체결(fills)로 T/수량/평단/현금을 갱신하고 요약을 반환한다.

    fills: [{"type": "star_buy", "qty": 3, "price": 91.2}, ...]  (BUY/SELL 은 type 으로 판별)
    """
    warnings: List[str] = []
    T_before, was_reverse = state.T, state.is_reverse
    filled_types, buys, sells = set(), [], []
    for f in fills:
        q, p, t = int(f.get("qty", 0)), float(f.get("price", 0)), f.get("type")
        if q <= 0 or p <= 0 or t not in BUY_TYPES | SELL_TYPES:
            continue
        filled_types.add(t)
        (buys if t in BUY_TYPES else sells).append((t, q, p))

    cycle_was_empty = state.hold_qty == 0
    if cycle_was_empty and buys:
        state.cycle_start_cash = state.cash
        state.cycle_buy_amt = 0.0

    sell_qty = sell_amt = 0
    for _, q, p in sells:                       # 매도 먼저 (평단은 매도로 변하지 않음)
        if q > state.hold_qty:
            warnings.append(f"보유수량({state.hold_qty})보다 많은 매도 체결({q}) - 보유수량으로 보정")
            q = state.hold_qty
        state.hold_qty -= q
        state.cash += q * p
        sell_qty += q
        sell_amt += q * p

    buy_qty = buy_amt = 0
    for _, q, p in buys:
        cost = q * p
        total_cost = state.hold_qty * state.avg_price + cost
        state.hold_qty += q
        state.avg_price = total_cost / state.hold_qty
        state.cash -= cost
        state.cycle_buy_amt += cost
        buy_qty += q
        buy_amt += cost
    if state.cash < -1e-6:
        warnings.append(f"현금이 음수({state.cash:.2f}) - 증권사 잔고와 대조 필요")

    state.T = transition_T(T_before, was_reverse, filled_types, splits)

    cycle_end = state.hold_qty == 0 and (bool(sells) or not cycle_was_empty)
    summary = {
        "filled_types": sorted(filled_types),
        "buy_qty": buy_qty, "buy_price": (buy_amt / buy_qty) if buy_qty else 0.0,
        "sell_qty": sell_qty, "sell_price": (sell_amt / sell_qty) if sell_qty else 0.0,
        "T_before": T_before, "cycle_end": cycle_end, "warnings": warnings,
        "cycle_profit": 0.0, "cycle_buy_amt": state.cycle_buy_amt, "cycle_return_pct": 0.0,
    }
    if cycle_end:
        profit = state.cash - state.cycle_start_cash
        summary["cycle_profit"] = profit
        summary["cycle_return_pct"] = (profit / state.cycle_buy_amt * 100) if state.cycle_buy_amt else 0.0
        state.T, state.avg_price, state.hold_qty = 0.0, 0.0, 0
        state.is_reverse, state.reverse_day = False, 0
        state.cycle_start_cash, state.cycle_buy_amt = 0.0, 0.0
    elif was_reverse:
        state.reverse_day += 1

    state.pending = []
    return summary
