# V4 무한매수법 자동매매 (패치본)

## 변경 요약
- 정산은 **실제 체결**로만 T/수량/평단/현금 갱신 (mock 제거)
- T 전이 버그 수정, 후반전 +1.0, 리버스 감소율 `1-2/splits`, 리버스 첫날 MOC
- 주문수량/가격 반올림, 1회매수금 ≤ 현금, avg_buy 수량 수정
- 미국 ET 기준 스케줄(서머타임 자동), 휴장일 필터, 중복주문 방지(재시작 안전)
- 기본 DRY-RUN. 실주문은 `LIVE_TRADING=1`
- 상태 파일 절대경로 + 원자적 저장(.bak), 손상 시 조용히 초기화하지 않음
- 텔레그램 parse_mode 제거(`_` 400 에러 방지), 실패 로그

## 설치
pip install requests python-dotenv exchange_calendars pytest

## .env
MERITZ_APP_KEY / MERITZ_APP_SECRET / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
LIVE_TRADING=0                 # 1 이면 실주문
ORDER_TIME_ET=09:35  RECONCILE_TIME_ET=16:20
MERITZ_QUOTE_REALTIME=1        # 일봉 조회 dely_rltm_cls_code (1=실시간 "0", 0=지연 "1"). 실시간은 별도 신청 필요
MERITZ_EXCH_SOXL=0066:AX       # 종목별 거래소 지정(주문 벤더코드:시세코드). 기본: TQQQ 0537:OQ, SOXL 0066:AX
MERITZ_MAC=                    # 포털에 등록한 단말 MAC(12자리)이 있으면 지정

## 실행
python main.py --check                     # 실주문 전 API 점검 (주문 안 냄)
python bot_daemon.py                       # 데몬
python main.py --morning [--live] [TQQQ]   # 수동 주문 (기본 dry)
python main.py --aftermarket [--live]      # 수동 정산

## 체결 정산
기본: /trading/v1/overseas/orders/history 로 주문번호+접수시각을 매칭해 자동 정산, 못 찾으면 정산 중단(10분마다 재시도).
수동 대체: `manual_fills_<SYMBOL>.json` 을 두면 그걸 우선 사용
[{"type":"star_buy","qty":3,"price":91.2}]

## 알림과 기록
- 텔레그램: 시작 상태 / 주문 접수(LOC 매수·매도 등) / 장 마감 정산 / 사이클 종료 / 이상 알림 / 하루 1회 하트비트(21:00 KST, `HEARTBEAT_TIME_KST`)
- 누적수익률 = (보유수량 × 체결가 + 남은금액 − 사이클 시작현금) ÷ 누적매수액. 평가가는 체결가(체결이 없으면 당일 종가)
- 기록 파일(대시보드용, 데이터 폴더): `orders.csv`(주문) / `daily.csv`(정산) / `cycles.csv`(사이클). 컬럼이 바뀌면 기존 파일은 `.old_<시각>`으로 보존
- `python main.py --status [--send]` 로 언제든 장부·증권사 잔고 요약 확인

## 복리 운용
config.py 종목에 `"compound": True` 추가 (없으면 원금 기준)

## 테스트
python tests/test_v4_strategy.py && python tests/test_meritz_wrapper.py && python tests/test_daemon_flow.py
