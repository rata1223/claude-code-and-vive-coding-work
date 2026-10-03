// The symbols the operator may put in the house strategy's universe. Mirrors
// backend/quant/data/universe.py (UNIVERSE); the server validates against that
// list, this only builds the picker. frontend/ and mobile/ carry identical
// copies (tests/integration/test_frontend_operator_screen.py).
export const UNIVERSE_GROUPS = [
  { key: 'us_etf', symbols: ['SPY', 'QQQ', 'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLU', 'XLRE'] },
  { key: 'us_large', symbols: ['AAPL', 'NVDA', 'MSFT', 'GOOGL', 'AMZN', 'META', 'TSLA', 'AVGO', 'JPM', 'V'] },
  { key: 'kr_etf', symbols: ['069500', '360750', '091160'] }
]

export const TRADING_UNIVERSE = UNIVERSE_GROUPS.flatMap((g) => g.symbols)
