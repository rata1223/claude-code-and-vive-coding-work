"""
매매 유니버스 상수 — 공통 import 지점.

이 파일이 KR_ETF, EXCD_MAP의 canonical source.
strategy/signals.py 와 backend/brokers/kis.py 모두 여기서 import.
"""

US_ETF = ["SPY", "QQQ", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE"]
US_LARGE = ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "JPM", "V"]
KR_ETF = ["069500", "360750", "091160"]  # KODEX200, TIGER S&P500, KODEX반도체

UNIVERSE = US_ETF + US_LARGE + KR_ETF

# KIS 해외주식 거래소 코드 (EXCD) 매핑
#
# ⚠️ 미확인 사항 — SPY와 XL* 섹터 ETF는 NYSE Arca 상장이다. KIS 주문 코드
# 집합에는 ARCA가 없고 NASD/NYSE/AMEX뿐인데, KIS가 Arca를 NYSE로 두는지
# AMEX로 두는지는 확인하지 못했다(종목 마스터 파일 다운로드가 이 환경의
# egress 정책에 막힘). 아래 NYSE 매핑은 이 변경 이전부터 있던 값을 그대로
# 둔 것이지 검증된 값이 아니다. 모의투자에서 이 종목들 주문이 거부되면
# 여기부터 의심할 것 — AMEX/AMS로 바꾸려면 CANONICAL_EXCHANGES와
# _QUOTE_EXCD에도 AMEX를 추가해야 한다.
EXCD_MAP: dict[str, str] = {s: "NASD" for s in ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN",
                                                   "META", "TSLA", "AVGO", "QQQ", "XLK", "XLRE"]}
EXCD_MAP.update({s: "NYSE" for s in ["SPY", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU",
                                       "JPM", "V"]})

# UNIVERSE 밖이지만 앱 종목 피커(api/routers/watchlist.py HOT_SYMBOLS)가 파는 종목.
# EXCD_MAP은 유니버스 목록이 아니라 심볼→거래소 매핑이므로, 주문 가능한 종목은
# 전부 여기 있어야 한다. 빠지면 미국 기본값(NASD)으로 떨어져 KIS가 주문을 거부한다.
EXCD_MAP.update({s: "NYSE" for s in ["BRK.B", "XOM", "WMT"]})
