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
EXCD_MAP: dict[str, str] = {s: "NASD" for s in ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN",
                                                   "META", "TSLA", "AVGO", "QQQ", "XLK", "XLRE"]}
EXCD_MAP.update({s: "NYSE" for s in ["SPY", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU",
                                       "JPM", "V"]})

# UNIVERSE 밖이지만 앱 종목 피커(api/routers/watchlist.py HOT_SYMBOLS)가 파는 종목.
# EXCD_MAP은 유니버스 목록이 아니라 심볼→거래소 매핑이므로, 주문 가능한 종목은
# 전부 여기 있어야 한다. 빠지면 미국 기본값(NASD)으로 떨어져 KIS가 주문을 거부한다.
EXCD_MAP.update({s: "NYSE" for s in ["BRK.B", "XOM", "WMT"]})
