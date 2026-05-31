import dataclasses
from datetime import time

from .models import BrokerCapabilities

KIS_LIVE_CAPABILITIES = BrokerCapabilities(
    broker_id="kis",
    market="US",
    currency="USD",
    supports_market_buy=False,   # ORD_DVSN="00" is hardcoded — true market orders not sent
    supports_market_sell=False,
    supports_limit_order=True,
    supports_stop_order=False,
    supports_fractional=False,
    supports_short=False,
    supports_after_hours=False,  # KIS rejects orders outside 22:30-05:00 KST
    supports_websocket=False,    # HTTP polling only
    supports_realtime_quote=False,
    supports_account_balance=True,
    supports_portfolio=True,
    fill_mechanism="polling",
    price_type="decimal",
    requires_exchange_code=True,
    requires_hashkey=True,
    cancel_requires_symbol=True,
    cancel_requires_qty_price=True,
    retry_safe_on_submit=False,
    rate_limit_per_sec=15,
    settlement_days=2,
    has_securities_tax=False,
    securities_tax_rate=0.0,
    price_precision={"US": 2},
    min_order_qty=1,
    session_open_kst=time(22, 30),
    session_close_kst=time(5, 0),
)

KIS_PAPER_CAPABILITIES = dataclasses.replace(KIS_LIVE_CAPABILITIES, rate_limit_per_sec=5)

KIWOOM_CAPABILITIES = BrokerCapabilities(
    broker_id="kiwoom",
    market="KR",
    currency="KRW",
    supports_market_buy=True,
    supports_market_sell=True,
    supports_limit_order=True,
    supports_stop_order=False,
    supports_fractional=False,
    supports_short=False,
    supports_after_hours=False,
    supports_websocket=True,     # real-time fill push via WebSocket
    supports_realtime_quote=False,
    supports_account_balance=False,  # stub — not yet implemented
    supports_portfolio=False,
    fill_mechanism="websocket",
    price_type="integer",
    requires_exchange_code=False,
    requires_hashkey=False,
    cancel_requires_symbol=False,
    cancel_requires_qty_price=False,
    retry_safe_on_submit=False,
    rate_limit_per_sec=5,
    settlement_days=2,
    has_securities_tax=True,
    securities_tax_rate=0.002,
    price_precision={"KR": 0},
    min_order_qty=1,
    session_open_kst=time(9, 5),
    session_close_kst=time(15, 25),
)

SIMULATOR_CAPABILITIES = BrokerCapabilities(
    broker_id="simulator",
    market="simulation",
    currency="KRW",
    supports_market_buy=True,
    supports_market_sell=True,
    supports_limit_order=True,
    supports_stop_order=False,
    supports_fractional=False,
    supports_short=False,
    supports_after_hours=True,   # no session constraints in simulation
    supports_websocket=False,
    supports_realtime_quote=False,
    supports_account_balance=True,
    supports_portfolio=True,
    fill_mechanism="sync",
    price_type="float",
    requires_exchange_code=False,
    requires_hashkey=False,
    cancel_requires_symbol=False,
    cancel_requires_qty_price=False,
    retry_safe_on_submit=True,
    rate_limit_per_sec=9999,
    settlement_days=0,
    has_securities_tax=True,
    securities_tax_rate=0.002,
    price_precision={"SIM": 4},
    min_order_qty=1,
    session_open_kst=None,
    session_close_kst=None,
)
