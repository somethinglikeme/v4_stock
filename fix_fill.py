"""장부에 반영되지 않은 체결을 수동으로 반영한다 (정산 누락 복구용).

  python fix_fill.py TQQQ first_buy 6 73.82
  python fix_fill.py TQQQ star_buy 3 83.10

- 반드시 데몬을 종료한 뒤 실행 (데몬이 상태 파일을 덮어쓸 수 있음)
- 실행 전 state_<SYMBOL>.json.bak_before_fix 로 백업, 정산 로직(reconcile)을 그대로 사용
- 가격은 메리츠 앱 '체결내역'의 체결단가를 사용 (없으면 증권사 평균단가)
"""
import shutil
import sys

from config import get_config
from storage import load_state, save_state, path_for
from v4_strategy import reconcile, BUY_TYPES, SELL_TYPES

if len(sys.argv) != 5:
    sys.exit(__doc__)
sym, typ, qty, price = sys.argv[1].upper(), sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
if typ not in BUY_TYPES | SELL_TYPES:
    sys.exit(f"type 은 {sorted(BUY_TYPES | SELL_TYPES)} 중 하나")
cfg = get_config(sym)
st = load_state(sym, cfg["principal"])
if st.pending:
    sys.exit("미정산(pending) 주문이 있습니다. 데몬이 정산하도록 두거나 manual_fills 를 사용하세요.")
print("반영 전:", {k: getattr(st, k) for k in ("T", "cash", "hold_qty", "avg_price", "is_reverse")})
shutil.copy2(path_for(sym), path_for(sym) + ".bak_before_fix")
s = reconcile(st, [{"type": typ, "qty": qty, "price": price}], cfg["splits"])
save_state(sym, st)
print("반영 후:", {k: getattr(st, k) for k in ("T", "cash", "hold_qty", "avg_price", "is_reverse")})
print("경고:", s["warnings"] or "없음")
