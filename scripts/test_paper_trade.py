"""
모의투자 드라이런 스크립트 — 실제 주문 없이 신호·비중 계산 결과를 출력한다.
실행: docker exec kis-bot python scripts/test_paper_trade.py
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"


def main():
    print("=== 모의투자 드라이런 ===")
    print(f"DRY_RUN={'켜짐 (주문 없음)' if DRY_RUN else '꺼짐 (실제 주문 실행)'}\n")

    from strategy.signals import TradingSignals, KR_ETF, US_ETF, US_LARGE
    from strategy.optimizer import PortfolioOptimizer

    signals = TradingSignals()
    optimizer = PortfolioOptimizer()

    # 신호 스캔 (시간이 걸릴 수 있음)
    sample_universe = ["SPY", "QQQ", "AAPL", "MSFT"]
    print(f"신호 스캔 중... (샘플: {sample_universe})")

    buy_candidates = []
    for symbol in sample_universe:
        try:
            df = signals.fetch_ohlcv(symbol)
            if signals.buy_signal(df):
                buy_candidates.append(symbol)
                print(f"  ✅ 매수 신호: {symbol}")
            else:
                print(f"  — 신호 없음: {symbol}")
        except Exception as e:
            print(f"  ❌ 오류 {symbol}: {e}")

    if not buy_candidates:
        print("\n현재 매수 신호 없음. 종료.")
        return

    # 비중 계산
    TOTAL_CAPITAL = 2_000_000
    print(f"\n비중 계산 중... (총 자본: {TOTAL_CAPITAL:,}원 기준)")
    weights = optimizer.compute_weights(buy_candidates, TOTAL_CAPITAL)

    print("\n[주문 계획]")
    for symbol, amount in weights.items():
        print(f"  {symbol}: {amount:,.0f}원 투자")

    if DRY_RUN:
        print("\nDRY_RUN=true — 실제 주문을 건너뜁니다.")
        print("실제 주문하려면: DRY_RUN=false python scripts/test_paper_trade.py")
        return

    # 실제 주문 (DRY_RUN=false 일 때만)
    from kis_adapter.market_data import KISMarketData
    from kis_adapter.orders import KISOrders
    from strategy.signals import EXCD_MAP

    md = KISMarketData()
    orders = KISOrders()

    for symbol, amount_krw in weights.items():
        if symbol in KR_ETF:
            price = md.get_price_kr(symbol)
            qty = max(1, int(amount_krw / price))
            orders.buy_kr(symbol, qty, price)
            print(f"주문 완료: {symbol} {qty}주 @ {price:,}원")
        else:
            excd = EXCD_MAP.get(symbol, "NASD")
            price = md.get_price_us(symbol, excd)
            qty = max(1, int(amount_krw / (price * 1350)))
            orders.buy_us(symbol, excd, qty, price)
            print(f"주문 완료: {symbol} {qty}주 @ ${price:.2f}")


if __name__ == "__main__":
    main()
