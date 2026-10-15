import json
import os
import shutil
from dataclasses import asdict, fields

from v4_strategy import V4State

# 실행 위치와 무관하게 항상 같은 폴더를 쓴다. 바꾸려면 환경변수 V4_DATA_DIR
BASE_DIR = os.getenv("V4_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
_FIELDS = {f.name for f in fields(V4State)}


def path_for(symbol):
    return os.path.join(BASE_DIR, f"state_{symbol.upper()}.json")


def load_state(symbol, principal):
    p = path_for(symbol)
    if not os.path.exists(p):
        print(f"[storage] {p} 없음 -> 신규 상태(cash={principal})로 시작")
        return V4State(cash=principal)
    with open(p, "r", encoding="utf-8") as f:
        d = json.load(f)  # 손상된 파일은 예외 발생 (조용히 초기화하지 않음)
    return V4State(**{k: v for k, v in d.items() if k in _FIELDS})


def save_state(symbol, state: V4State):
    p = path_for(symbol)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(p):
        shutil.copy2(p, p + ".bak")
    os.replace(tmp, p)  # 원자적 교체
