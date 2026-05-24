import logging
from typing import TYPE_CHECKING

from backend.strategy.base import StrategyBase
from backend.strategy.script.sandbox import SandboxViolation, execute_script, validate_script

if TYPE_CHECKING:
    from backend.brokers.base import BrokerAdapter
    from backend.execution.position_tracker import Fill, PositionTracker

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE = '''
# KIS Trading 전략 템플릿
# 사용 가능: self.buy(symbol, qty), self.sell(symbol, qty), self.get_price(symbol)
# bar = {"symbol": str, "open": float, "high": float, "low": float, "close": float, "volume": float}

def on_bar(self, bar):
    price = bar["close"]
    symbol = bar["symbol"]
    positions = self.get_positions()
    holding = any(p.symbol == symbol for p in positions)

    if not holding and price > 0:
        self.buy(symbol, 1, price)
'''


class ScriptStrategy(StrategyBase):
    """
    사용자 Python 스크립트를 샌드박스에서 실행하는 전략.
    스크립트는 on_bar(self, bar) 함수를 포함해야 함.
    """

    def __init__(self, broker: "BrokerAdapter", tracker: "PositionTracker",
                 name: str, script: str, config: dict = None):
        super().__init__(broker, name)
        self._tracker = tracker
        self._config = config or {}
        self._script = script
        self._validate()

    def _validate(self):
        try:
            validate_script(self._script)
            logger.info("[%s] 스크립트 검증 통과", self.name)
        except SandboxViolation as e:
            logger.error("[%s] 스크립트 검증 실패: %s", self.name, e)
            raise

    def on_start(self):
        logger.info("[%s] ScriptStrategy 시작", self.name)

    def on_bar(self, bar: dict):
        context = {
            "self": self,
            "bar": bar,
        }
        try:
            execute_script(self._script + f"\non_bar(self, bar)", context, timeout_sec=5)
        except TimeoutError:
            logger.error("[%s] on_bar 타임아웃 (symbol=%s)", self.name, bar.get("symbol"))
        except SandboxViolation as e:
            logger.error("[%s] 샌드박스 위반: %s", self.name, e)
        except Exception as e:
            logger.warning("[%s] on_bar 오류: %s", self.name, e)

    def on_fill(self, fill: "Fill"):
        context = {
            "self": self,
            "fill": fill,
        }
        script_with_call = self._script + "\nif 'on_fill' in dir(): on_fill(self, fill)"
        try:
            execute_script(script_with_call, context, timeout_sec=3)
        except Exception:
            pass

    def on_stop(self):
        logger.info("[%s] ScriptStrategy 중단", self.name)
