"""
KIS API 연결 테스트 스크립트
실행: docker exec kis-bot python scripts/test_connection.py
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()


def check_env():
    required = ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"]
    missing = [k for k in required if not os.environ.get(k) or os.environ[k].startswith("여기에")]
    if missing:
        print(f"❌ 환경변수 미설정: {', '.join(missing)}")
        print("   .env 파일에 실제 값을 입력하세요.")
        sys.exit(1)
    print(f"✅ 환경변수 확인 완료 (KIS_ENV={os.environ.get('KIS_ENV', 'paper')})")


def test_token():
    from kis_adapter.auth import KISAuth
    auth = KISAuth()
    try:
        token = auth.get_token()
        print(f"✅ 토큰 발급 성공 (앞 20자: {token[:20]}...)")
        return auth
    except Exception as e:
        print(f"❌ 토큰 발급 실패: {e}")
        sys.exit(1)


def test_price_us():
    from kis_adapter.market_data import KISMarketData
    md = KISMarketData()
    try:
        price = md.get_price_us("SPY", "AMEX")
        print(f"✅ 미국 시세 조회 성공: SPY = ${price:.2f}")
    except Exception as e:
        print(f"❌ 미국 시세 조회 실패: {e}")


def test_price_kr():
    from kis_adapter.market_data import KISMarketData
    md = KISMarketData()
    try:
        price = md.get_price_kr("069500")
        print(f"✅ 한국 시세 조회 성공: KODEX 200 = {price:,}원")
    except Exception as e:
        print(f"❌ 한국 시세 조회 실패: {e}")


def test_balance():
    from kis_adapter.portfolio import KISPortfolio
    port = KISPortfolio()
    try:
        kr = port.get_kr_balance()
        positions = kr["positions"]
        print(f"✅ 한국 잔고 조회 성공: 보유 종목 {len(positions)}개")
    except Exception as e:
        print(f"❌ 한국 잔고 조회 실패: {e}")


if __name__ == "__main__":
    print("=== KIS API 연결 테스트 ===\n")
    check_env()
    test_token()
    test_price_us()
    test_price_kr()
    test_balance()
    print("\n테스트 완료. ❌ 없으면 모의투자 실행 가능합니다.")
