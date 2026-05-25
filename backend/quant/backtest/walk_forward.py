"""
Deliverable 6 (WFO): Walk-Forward Optimization + Out-of-Sample Validation.
Expanding window (anchored) 또는 Rolling window 방식 지원.
각 IS 구간에서 최적 파라미터 탐색 → OOS 구간에서 검증.
"""
import logging
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from backend.quant.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from backend.quant.backtest.metrics import summarize, max_drawdown, sharpe

logger = logging.getLogger(__name__)


@dataclass
class WFOWindow:
    is_start: str   # In-Sample 시작
    is_end: str     # In-Sample 종료
    oos_start: str  # Out-of-Sample 시작
    oos_end: str    # Out-of-Sample 종료


@dataclass
class WFOResult:
    windows: list[WFOWindow]
    oos_results: list[BacktestResult]
    combined_equity: pd.Series
    combined_metrics: dict
    best_params_per_window: list[dict]


def _make_windows(df: pd.DataFrame, is_bars: int, oos_bars: int,
                  anchored: bool = True) -> list[WFOWindow]:
    """IS/OOS 윈도우 생성."""
    windows = []
    idx = df.index
    n = len(idx)
    start = 0
    while start + is_bars + oos_bars <= n:
        is_s = str(idx[start].date())
        is_e = str(idx[start + is_bars - 1].date())
        oos_s = str(idx[start + is_bars].date())
        oos_e = str(idx[min(start + is_bars + oos_bars - 1, n - 1)].date())
        windows.append(WFOWindow(is_s, is_e, oos_s, oos_e))
        if anchored:
            start += oos_bars  # IS 시작은 고정, OOS만 전진
        else:
            start += oos_bars  # Rolling: IS/OOS 모두 전진
    return windows


class WalkForwardOptimizer:
    """
    Walk-Forward Optimization.

    사용 예:
        def signal_fn(df, params):
            # params: {"fast": 50, "slow": 200}
            from backend.quant.indicators.trend import sma_cross
            return sma_cross(df, fast=params["fast"], slow=params["slow"]).signal

        param_grid = [{"fast": f, "slow": s} for f in [20,50] for s in [100,200]]
        wfo = WalkForwardOptimizer(signal_fn, param_grid)
        result = wfo.run(df, is_bars=504, oos_bars=126)
    """

    def __init__(
        self,
        signal_fn: Callable[[pd.DataFrame, dict], pd.Series],
        param_grid: list[dict],
        config: Optional[BacktestConfig] = None,
        metric: str = "sharpe",    # IS 최적화 기준: sharpe | calmar | total_return_pct
        anchored: bool = True,
    ):
        self.signal_fn = signal_fn
        self.param_grid = param_grid
        self.config = config or BacktestConfig()
        self.metric = metric
        self.anchored = anchored
        self._engine = BacktestEngine(self.config)

    def _score(self, result: BacktestResult) -> float:
        return result.metrics.get(self.metric, 0.0)

    def _best_params(self, df_is: pd.DataFrame) -> tuple[dict, float]:
        best_p, best_s = self.param_grid[0], -999.0
        for params in self.param_grid:
            try:
                sig = self.signal_fn(df_is, params)
                result = self._engine.run(df_is, sig)
                score = self._score(result)
                if score > best_s:
                    best_s, best_p = score, params
            except Exception as e:
                logger.debug("WFO param eval failed %s: %s", params, e)
        return best_p, best_s

    def run(self, df: pd.DataFrame, is_bars: int = 504,
            oos_bars: int = 126, symbol: str = "") -> WFOResult:
        """
        is_bars: IS 구간 봉 수 (기본 2년 = 504)
        oos_bars: OOS 구간 봉 수 (기본 6개월 = 126)
        """
        windows = _make_windows(df, is_bars, oos_bars, self.anchored)
        if not windows:
            raise ValueError(f"데이터 부족: {len(df)}봉, IS={is_bars}, OOS={oos_bars} 필요")

        oos_results = []
        best_params_list = []
        oos_equities = []

        for win in windows:
            df_is = df.loc[win.is_start:win.is_end]
            df_oos = df.loc[win.oos_start:win.oos_end]

            best_p, is_score = self._best_params(df_is)
            best_params_list.append({**best_p, "_is_score": round(is_score, 4)})
            logger.info("WFO IS %s~%s best=%s score=%.3f", win.is_start, win.is_end, best_p, is_score)

            try:
                oos_sig = self.signal_fn(df_oos, best_p)
                oos_r = self._engine.run(df_oos, oos_sig, symbol=symbol)
                oos_results.append(oos_r)
                oos_equities.append(oos_r.equity_curve)
                logger.info("WFO OOS %s~%s Sharpe=%.2f MDD=%.1f%%",
                            win.oos_start, win.oos_end,
                            oos_r.metrics.get("sharpe", 0),
                            oos_r.metrics.get("max_drawdown_pct", 0))
            except Exception as e:
                logger.warning("WFO OOS run failed: %s", e)

        if not oos_equities:
            raise RuntimeError("WFO: OOS 결과 없음")

        # OOS 구간 연결 (각 구간 시작을 이전 구간 끝 자산으로 스케일)
        combined = _chain_equity(oos_equities, self.config.initial_capital)
        all_trades = [t.__dict__ for r in oos_results for t in r.trades]
        combined_metrics = summarize(combined, all_trades, self.config.initial_capital)
        combined_metrics["wfo_windows"] = len(windows)
        combined_metrics["wfo_oos_bars"] = oos_bars

        return WFOResult(
            windows=windows,
            oos_results=oos_results,
            combined_equity=combined,
            combined_metrics=combined_metrics,
            best_params_per_window=best_params_list,
        )


def lookahead_bias_check(
    signal_fn,
    df: pd.DataFrame,
    params: dict,
    future_window: int = 5,
) -> dict:
    """
    룩어헤드 바이어스 탐지기.

    방법: 마지막 future_window 봉을 제거한 데이터로 신호를 재계산.
    신호 값이 전체 데이터 기준과 다르면 해당 신호가 미래 데이터를 사용한 것.

    NaN 마스킹 방식 대신 데이터 절단(truncation)을 사용해
    rolling window 의 부작용(false positive)을 방지.
    """
    try:
        if len(df) <= future_window + 1:
            return {"error": "insufficient_data"}

        sig_full = signal_fn(df, params)

        # 마지막 future_window 봉 제거 후 재계산
        df_truncated = df.iloc[:-future_window]
        sig_truncated = signal_fn(df_truncated, params)

        # 절단 데이터의 마지막 신호 vs 전체 데이터의 같은 위치 신호 비교
        check_idx = min(len(sig_truncated), len(sig_full) - future_window)
        if check_idx <= 0:
            return {"error": "insufficient_overlap"}

        # 전체에서 마지막 future_window 이전의 신호들과 비교
        full_tail = sig_full.iloc[-future_window - 1:-future_window]
        trunc_tail = sig_truncated.iloc[-1:]

        mismatch = 0
        for a, b in zip(full_tail.values, trunc_tail.values):
            if a != a or b != b:  # nan-safe
                continue
            if a != b:
                mismatch += 1

        bias_detected = mismatch > 0

        return {
            "lookahead_bias_detected": bias_detected,
            "mismatch_bars": mismatch,
            "future_window": future_window,
            "warning": "룩어헤드 바이어스 의심" if bias_detected else "정상",
        }
    except Exception as e:
        return {"error": str(e)}


def reject_overfitting(
    is_score: float,
    oos_score: float,
    degradation_threshold: float = 0.5,
) -> dict:
    """
    IS vs OOS 성과 비교로 과적합 탐지.
    OOS 성과가 IS 대비 degradation_threshold 이하이면 과적합 의심.

    예: IS Sharpe=2.0, OOS Sharpe=0.4 → 0.4/2.0 = 0.2 < 0.5 → 과적합
    """
    if is_score == 0:
        return {"overfitting": False, "ratio": None, "warning": "IS score=0"}

    ratio = oos_score / is_score if is_score > 0 else 0.0
    overfit = ratio < degradation_threshold

    return {
        "overfitting": overfit,
        "is_score": round(is_score, 4),
        "oos_score": round(oos_score, 4),
        "ratio": round(ratio, 4),
        "warning": f"과적합 의심 (OOS/IS={ratio:.2f} < {degradation_threshold})" if overfit else "정상",
    }


def _chain_equity(equities: list[pd.Series], initial: float) -> pd.Series:
    """OOS 구간별 equity curve를 연속으로 이어붙임."""
    parts = []
    scale = initial
    for eq in equities:
        if eq.empty:
            continue
        ratio = scale / eq.iloc[0]
        scaled = eq * ratio
        parts.append(scaled)
        scale = scaled.iloc[-1]
    if not parts:
        return pd.Series(dtype=float)
    return pd.concat(parts)


def monte_carlo_robustness(result: BacktestResult, n_simulations: int = 1000,
                           seed: int = 42) -> dict:
    """
    Deliverable 5 (검증): Monte Carlo 수익률 시뮬레이션.
    실제 거래 수익률을 무작위 셔플 → 분포 확인으로 전략 견고성 검증.
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    trades = result.trades
    if not trades:
        return {"error": "no_trades"}

    pnls = [t.pnl for t in trades]
    sim_sharpes = []
    sim_mdds = []

    for _ in range(n_simulations):
        shuffled = rng.choice(pnls, size=len(pnls), replace=True)
        eq = pd.Series(result.config.initial_capital + shuffled.cumsum())
        from backend.quant.backtest.metrics import sharpe as sharpe_fn, max_drawdown as mdd_fn
        sim_sharpes.append(sharpe_fn(eq))
        sim_mdds.append(mdd_fn(eq))

    sim_sharpes = np.array(sim_sharpes)
    sim_mdds = np.array(sim_mdds)

    return {
        "sharpe_mean": round(float(sim_sharpes.mean()), 3),
        "sharpe_5th_pct": round(float(np.percentile(sim_sharpes, 5)), 3),
        "sharpe_95th_pct": round(float(np.percentile(sim_sharpes, 95)), 3),
        "mdd_mean_pct": round(float(sim_mdds.mean()) * 100, 2),
        "mdd_worst_pct": round(float(np.percentile(sim_mdds, 5)) * 100, 2),
        "n_simulations": n_simulations,
        "actual_sharpe": result.metrics.get("sharpe", 0),
        "actual_mdd_pct": result.metrics.get("max_drawdown_pct", 0),
    }
