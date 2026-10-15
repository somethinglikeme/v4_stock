# config.py - 포트폴리오 설정 (여기만 수정)
# 투자종목, 분할횟수(20,30,40), 목표수익률(%), 투자원금(USD) 설정
PORTFOLIO = {
    "TQQQ": {"splits": 20, "target_pct": 15, "principal": 10000},
   # "TQQQ": {"splits": 40, "target_pct": 15, "principal": 20000},
   # "SOXL": {"splits": 20, "target_pct": 20, "principal": 15000},
    # 예시 커스텀 TIGER
    # "UPRO": {"splits": 30, "target_pct": 12, "principal": 10000},
}

DEFAULT_TARGET_MAP = {
    "TQQQ": 15, "SOXL": 20, "UPRO": 12, "TECL": 15, "QLD": 10, "FNGU": 15
}
DEFAULT_SPLITS = 40
DEFAULT_PRINCIPAL = 10000

def get_config(symbol: str):
    s = symbol.upper()
    if s in PORTFOLIO:
        return PORTFOLIO[s]
    return {
        "splits": DEFAULT_SPLITS,
        "target_pct": DEFAULT_TARGET_MAP.get(s, 10),
        "principal": DEFAULT_PRINCIPAL
    }

def add_tiger(symbol: str, splits: int, target_pct: float, principal: float):
    assert splits in [20,30,40], "분할은 20/30/40만 가능"
    PORTFOLIO[symbol.upper()] = {"splits": splits, "target_pct": target_pct, "principal": principal}
