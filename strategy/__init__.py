# Workaround for the pandas-ta-openbb fork (imported as `pandas_ta`): its maps.py references
# importlib.metadata without importing it, raising AttributeError on a clean interpreter.
# Must run before `.signals` (which imports pandas_ta). Remove once upstream fixes maps.py.
import importlib.metadata  # noqa: F401

from .signals import TradingSignals
from .optimizer import PortfolioOptimizer
from .risk import RiskManager
from .indicator_strategy import IndicatorStrategy, IndicatorConfig, Condition, SUPPORTED_INDICATORS
from .script_strategy import ScriptStrategy, Bar, Signal, Order, SCRIPT_TEMPLATES
from .backtest import Backtester

__all__ = [
    "TradingSignals", "PortfolioOptimizer", "RiskManager",
    "IndicatorStrategy", "IndicatorConfig", "Condition", "SUPPORTED_INDICATORS",
    "ScriptStrategy", "Bar", "Signal", "Order", "SCRIPT_TEMPLATES",
    "Backtester",
]
