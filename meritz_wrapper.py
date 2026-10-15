"""메리츠증권 Open API 래퍼 (명세 meritz_openapi_spec_20261012.json 기준)

- 오류는 HTTP 500 + rsp_cd 로 내려오므로 상태코드가 아니라 rsp_cd 로 성공/실패를 판정한다.
- 주문 성공 rsp_cd = "0001", 조회류는 API마다 다름("5766"/"5820"/"0000").
- LOC(32)/MOC(34)는 네이티브 지원 -> 에뮬레이션 없음.
"""
import hashlib
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

APP_KEY = os.getenv("MERITZ_APP_KEY", "")
APP_SECRET = os.getenv("MERITZ_APP_SECRET", "")
BASE_URL = os.getenv("MERITZ_BASE_URL", "https://openapi.imeritz.com:9443")
# 명세상 12자리 MAC 필수. 포털에 등록한 단말 MAC이 있으면 MERITZ_MAC 으로 지정
MAC_ADDRESS = (os.getenv("MERITZ_MAC") or
               (hashlib.md5(f"v4local-{APP_KEY}".encode()).hexdigest()[:12].upper() if APP_KEY else "000000000000"))
HAS_MERITZ = bool(APP_KEY and APP_SECRET)

ET, KST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")
QUOTE_REALTIME = os.getenv("MERITZ_QUOTE_REALTIME", "1") == "1"   # dely_rltm_cls_code "0"(실시간, 별도 신청) / "1"(지연)

# 종목 -> (주문용 거래소 벤더코드, 시세용 거래소코드). SOXL(NYSE Arca)이 메리츠에서 어느 쪽인지는 --check 로 확인
EXCHANGES = {"TQQQ": ("0537", "OQ"), "SOXL": ("0066", "AX")}


def exch_for(symbol):
    env = os.getenv(f"MERITZ_EXCH_{symbol.upper()}")        # 예) MERITZ_EXCH_SOXL=0321:NY
    if env:
        v, c = env.split(":")
        return v, c
    if symbol.upper() not in EXCHANGES:
        raise KeyError(f"{symbol}: 거래소 매핑 없음 - MERITZ_EXCH_{symbol.upper()}=<벤더코드>:<시세코드> 로 지정")
    return EXCHANGES[symbol.upper()]


_token = {"v": None, "exp": 0.0}


def get_meritz_token(force=False):
    if not HAS_MERITZ:
        print("[경고] MERITZ_APP_KEY 또는 MERITZ_APP_SECRET가 설정되지 않았습니다.")
        return None
    if not force and _token["v"] and time.time() < _token["exp"]:
        return _token["v"]
    try:
        res = requests.post(f"{BASE_URL}/oauth2/token", timeout=10,
                            headers={"content-type": "application/x-www-form-urlencoded"},
                            data={"grant_type": "client_credentials", "client_id": APP_KEY,
                                  "client_secret": APP_SECRET, "scope": "login"})
        res.raise_for_status()
        j = res.json()
        _token["v"] = j.get("access_token")
        _token["exp"] = time.time() + max(300.0, float(j.get("expires_in", 3600)) - 600.0)
        return _token["v"]
    except Exception as e:
        print("메리츠 토큰 발급 실패:", e)
        return None


def _call(method, path, params=None, body=None, retries=3):
    """공통 호출. (http_status, json) 반환. 초당 호출 제한(EGW00200)은 자동 재시도."""
    for attempt in range(retries):
        token = get_meritz_token()
        if not token:
            raise RuntimeError("메리츠 토큰 없음 - .env 의 키를 확인하세요")
        headers = {"authorization": f"Bearer {token}", "mac_address": MAC_ADDRESS,
                   "content-type": "application/json; charset=utf-8"}
        res = requests.request(method, f"{BASE_URL}{path}", params=params, json=body,
                               headers=headers, timeout=10)
        try:
            j = res.json()
        except ValueError:
            raise RuntimeError(f"{path} 비JSON 응답 HTTP {res.status_code}: {res.text[:200]}")
        if j.get("rsp_cd") == "EGW00200":
            time.sleep(1.0 * (attempt + 1))
            continue
        if res.status_code == 401:
            get_meritz_token(force=True)
            continue
        return res.status_code, j
    raise RuntimeError(f"{path} 호출 제한/인증 재시도 초과")


def _fail(path, j):
    return RuntimeError(f"{path} 실패 rsp_cd={j.get('rsp_cd')} {j.get('rsp_msg')} {j.get('rsp_sub_msg') or ''}")


# ------------------------------------------------------------------ 시세
def get_last_close_yfinance(symbol):
    """(직전 거래일 종가, 최근 5개 종가). 이름은 호환용. 진행 중인 당일 봉은 제외한다."""
    _, cls = exch_for(symbol)
    _, j = _call("GET", "/market/v1/overseas/candles/days", params={
        "mrkt_div_code": "OV", "mrkt_cls_code": cls, "iscd": symbol,
        "dely_rltm_cls_code": "0" if QUOTE_REALTIME else "1", "mod_stpr_cls_code": "1"})
    if j.get("rsp_cd") != "0000":
        raise _fail("candles/days", j)
    today_et = datetime.now(ET).strftime("%Y%m%d")
    rows = [r for r in (j.get("data") or []) if r.get("date") and r["date"] < today_et]
    rows.sort(key=lambda r: r["date"])
    closes = [(r["date"], float(r["prpr"])) for r in rows if float(r.get("prpr") or 0) > 0]
    if not closes:
        raise RuntimeError(f"{symbol} 일봉 데이터 없음 (거래소코드 {cls} 확인)")
    last_date = datetime.strptime(closes[-1][0], "%Y%m%d").date()
    if (datetime.now(ET).date() - last_date).days > 5:
        raise RuntimeError(f"{symbol} 일봉이 오래됨(최근 {closes[-1][0]}) - 지연시세/조회 설정 확인")
    vals = [c for _, c in closes]
    return vals[-1], (vals[-5:] if len(vals) >= 5 else [])


# ------------------------------------------------------------------ 주문
_CLS = {"LIMIT": ("01", "0"), "LOC": ("32", "7"), "MOC": ("34", "7")}


def place_overseas_order(symbol, side, qty, price=None, is_moc=False, kind="LIMIT"):
    """kind: LIMIT(01) | LOC(32) | MOC(34, 단가 0). 반환 {"ok", "order_no", ...}"""
    if qty <= 0:
        return {"ok": False, "msg": "수량 0"}
    if is_moc:
        kind = "MOC"
    if kind not in _CLS:
        return {"ok": False, "error": f"알 수 없는 주문종류 {kind}"}
    if kind != "MOC" and not price:
        return {"ok": False, "error": f"{kind} 주문에 가격 없음"}
    try:
        vendor, _ = exch_for(symbol)
        cls, cond = _CLS[kind]
        body = {"ovrs_stck_symb_iscd": symbol, "ovrs_exch_vndr_idno": vendor,
                "oder_cls_code": cls, "oder_cond_cls_code": cond, "odqt": int(qty),
                "ovrs_stck_oder_prc": "0" if kind == "MOC" else f"{float(price):.2f}"}
        path = "/trading/v1/overseas/orders/" + ("buy" if side == "BUY" else "sell")
        _, j = _call("POST", path, body=body, retries=1)   # 주문은 중복 위험 때문에 재시도 최소화
    except Exception as e:
        return {"ok": False, "error": str(e)}
    data = j.get("data") or {}
    if j.get("rsp_cd") != "0001":
        return {"ok": False, "error": f"rsp_cd={j.get('rsp_cd')} {j.get('rsp_msg')} {j.get('rsp_sub_msg') or ''}"}
    if str(data.get("warn_cls_code", "0")) != "0":
        # 1/3 은 확인 후 재전송하면 접수되지만 자동 승인하지 않는다. 2 는 재전송해도 불가.
        return {"ok": False, "error": f"경고 코드 {data.get('warn_cls_code')}: {data.get('warn_msg')} (자동 재전송 안 함)"}
    print(f"[ORDER] {symbol} {side} {qty} @ {body['ovrs_stck_oder_prc']} ({kind}) -> #{data.get('oder_no')}")
    return {"ok": True, "order_no": str(data.get("oder_no")), "result": j}


# ------------------------------------------------------------------ 체결 조회
def _history_rows(d_from, d_to):
    rows, tr_cont, key = [], None, None
    for _ in range(10):
        params = {"oder_sta_date": d_from, "oder_fin_date": d_to, "cntg_cls_code": "0"}
        if tr_cont == "1":
            params.update(tr_cont="1", tr_cont_key=key)
        _, j = _call("GET", "/trading/v1/overseas/orders/history", params=params)
        if j.get("rsp_cd") == "5820":
            break
        if j.get("rsp_cd") not in ("5766", "5762"):
            raise _fail("orders/history", j)
        rows += j.get("data") or []
        tr_cont, key = j.get("tr_cont"), j.get("tr_cont_key")
        if tr_cont != "1":
            break
    return rows


def get_order_results(orders):
    """orders: [{"order_no": "12", "submitted_at": "2026-10-13T22:35:10"(KST)}...]
    반환: {order_no: {"qty": 체결수량, "price": 체결단가, "rejected": bool, "reason": str}}
    조회 결과에서 주문을 못 찾으면 예외(체결 없음으로 간주하지 않는다).
    """
    if not orders:
        return {}
    def _kst(v):
        t = datetime.fromisoformat(v)
        return t.replace(tzinfo=KST) if t.tzinfo is None else t
    ts = [_kst(o["submitted_at"]) for o in orders]
    d_from = (min(ts) - timedelta(days=1)).strftime("%Y%m%d")
    d_to = (max(ts) + timedelta(days=1)).strftime("%Y%m%d")
    rows = _history_rows(d_from, d_to)
    out = {}
    for o, t in zip(orders, ts):
        cands = []
        for r in rows:
            if str(r.get("oder_no")) != str(o["order_no"]) or str(r.get("oder_rctf_cncl_cls_code", "0")) != "0":
                continue
            try:
                rt = datetime.strptime(r["oder_exec_ymdh"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            except Exception:
                continue
            if abs((rt - t).total_seconds()) <= 600:      # 같은 번호가 다른 날에도 있으므로 시각으로 구분
                cands.append(r)
        if not cands:
            raise RuntimeError(f"주문번호 {o['order_no']} 를 체결내역에서 찾지 못함 (조회 {d_from}~{d_to})")
        r = cands[0]
        rejected = str(r.get("ovrs_stck_oder_stat_cls_code", "0")) != "0"
        out[str(o["order_no"])] = {
            "qty": 0 if rejected else int(float(r.get("cntg_qty") or 0)),
            "price": float(r.get("cntg_crnc_prc") or 0),
            "rejected": rejected, "reason": r.get("ovrs_stck_rfus_reas") or ""}
    return out


# ------------------------------------------------------------------ 잔고
def get_position(symbol):
    """증권사 보유수량(해당 종목). 잔고 대조용."""
    _, j = _call("GET", "/accounts/v1/me/overseas/holdings")
    if j.get("rsp_cd") not in ("5766", "5820"):
        raise _fail("overseas/holdings", j)
    for r in j.get("data") or []:
        if r.get("ovrs_stck_symb_iscd") == symbol.upper():
            return {"qty": float(r.get("rmnd_dqty") or 0), "avg": float(r.get("avrg_unpr") or 0)}
    return {"qty": 0.0, "avg": 0.0}


def get_usd_orderable():
    _, j = _call("GET", "/trading/v1/overseas/orders/estimate")
    if j.get("rsp_cd") not in ("5766", "5820"):
        raise _fail("orders/estimate", j)
    for r in j.get("data") or []:
        if r.get("crcd") == "USD":
            return float(r.get("oder_crnc_oder_able_camt") or 0)
    return 0.0
