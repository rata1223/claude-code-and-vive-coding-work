"""
포트폴리오 성과 분석.

1. 롤링 Sharpe / 롤링 드로다운
2. 전략 기여도 분해 (attribution)
3. 레짐별 성과 분류
4. 중복 전략 탐지
5. 인자(factor) 분해
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── 롤링 성과 지표 ─────────────────────────────────────────────────────────────

def rolling_sharpe(equity: pd.Series, window: int = 63,
                   risk_free_annual: float = 0.05) -> pd.Series:
    """
    rolling window 영업일 기준 Sharpe ratio.
    window=63 → 약 3개월.
    """
    rets = equity.pct_change()
    rf_daily = risk_free_annual / 252
    excess = rets - rf_daily
    roll_mean = excess.rolling(window).mean()
    roll_std = rets.rolling(window).std()
    return (roll_mean * 252) / (roll_std * np.sqrt(252) + 1e-9)


def rolling_drawdown(equity: pd.Series, window: int = 63) -> pd.Series:
    """rolling window 내 최대낙폭 (음수 분율)."""
    peak = equity.rolling(window, min_periods=1).max()
    return (equity - peak) / (peak + 1e-9)


def rolling_sortino(equity: pd.Series, window: int = 63,
                    risk_free_annual: float = 0.05) -> pd.Series:
    rets = equity.pct_change()
    rf_daily = risk_free_annual / 252
    excess = rets - rf_daily
    roll_mean = excess.rolling(window).mean()
    downside = rets.copy()
    downside[downside > 0] = 0.0
    roll_down_std = downside.rolling(window).std()
    return (roll_mean * 252) / (roll_down_std * np.sqrt(252) + 1e-9)


# ── 전략 기여도 분석 ──────────────────────────────────────────────────────────

@dataclass
class StrategyContribution:
    name: str
    avg_signal_strength: float    # 평균 신호 강도
    avg_score_contribution: float # 융합 score 기여분
    win_rate: float               # 신호 후 수익 방향 일치율
    regime_accuracy: dict         # 레짐별 정확도 {"trend": 0.6, "range": 0.4}
    redundancy_score: float       # 다른 전략과의 최대 상관계수


def compute_strategy_contributions(
    fusion_history: list[dict],  # [{symbol, signal, individual: {name: SignalOutput}}, ...]
    outcome_history: list[dict], # [{symbol, pnl}, ...]
) -> list[StrategyContribution]:
    """
    기록된 신호 이력과 실제 수익 이력으로 전략별 기여도 계산.

    fusion_history: RobustFusion.evaluate() 결과의 FusionResult.meta + individual
    outcome_history: 해당 신호의 체결 후 PnL
    """
    if not fusion_history or not outcome_history:
        return []

    strategy_signals: dict[str, list[tuple[int, float]]] = {}  # {name: [(signal, strength)]}
    strategy_outcomes: dict[str, list[float]] = {}

    for i, entry in enumerate(fusion_history):
        if i >= len(outcome_history):
            break
        pnl = outcome_history[i].get("pnl", 0.0)
        individual = entry.get("individual", {})
        for name, out in individual.items():
            strategy_signals.setdefault(name, []).append((out.signal, out.strength))
            strategy_outcomes.setdefault(name, []).append(pnl)

    contributions = []
    all_signal_series = {}

    for name, signals in strategy_signals.items():
        directions = [s for s, _ in signals]
        strengths = [st for _, st in signals]
        outcomes = strategy_outcomes.get(name, [])

        avg_strength = float(np.mean(strengths)) if strengths else 0.0

        # 신호 방향과 수익 방향 일치율
        matches = [
            1 for d, o in zip(directions, outcomes)
            if (d == 1 and o > 0) or (d == -1 and o < 0) or (d == 0)
        ]
        win_r = len(matches) / len(directions) if directions else 0.0

        # 신호 시계열 저장 (중복성 계산용)
        all_signal_series[name] = pd.Series(directions, dtype=float)

        contributions.append(StrategyContribution(
            name=name,
            avg_signal_strength=round(avg_strength, 4),
            avg_score_contribution=0.0,   # 아래에서 채움
            win_rate=round(win_r, 4),
            regime_accuracy={},
            redundancy_score=0.0,
        ))

    # 중복성 계산
    if len(all_signal_series) > 1:
        df_sig = pd.DataFrame(all_signal_series).dropna()
        corr = df_sig.corr()
        for contrib in contributions:
            others = [c for c in corr.columns if c != contrib.name]
            if others:
                max_corr = corr.loc[contrib.name, others].abs().max()
                contrib.redundancy_score = round(float(max_corr), 4)

    return sorted(contributions, key=lambda c: -c.win_rate)


# ── 레짐별 성과 분석 ──────────────────────────────────────────────────────────

@dataclass
class RegimePerformance:
    regime: str
    n_bars: int
    total_return_pct: float
    sharpe: float
    win_rate_pct: float
    avg_position_size: float


def regime_performance_breakdown(
    equity: pd.Series,
    regime_labels: pd.Series,  # DatetimeIndex, values "trend"|"range"|"stress"
) -> list[RegimePerformance]:
    """
    레짐별로 equity를 분할해 성과 지표를 계산.
    regime_labels와 equity의 인덱스가 일치해야 함.
    """
    from backend.quant.backtest.metrics import sharpe as sharpe_fn, win_rate

    common_idx = equity.index.intersection(regime_labels.index)
    if common_idx.empty:
        return []

    eq = equity.loc[common_idx]
    reg = regime_labels.loc[common_idx]
    results = []

    for regime in reg.unique():
        mask = reg == regime
        sub_eq = eq[mask]
        if len(sub_eq) < 5:
            continue

        ret = (sub_eq.iloc[-1] / sub_eq.iloc[0] - 1) * 100
        sh = sharpe_fn(sub_eq)
        rets = sub_eq.pct_change().dropna()
        w_rate = float((rets > 0).mean()) * 100

        results.append(RegimePerformance(
            regime=regime,
            n_bars=len(sub_eq),
            total_return_pct=round(float(ret), 2),
            sharpe=round(float(sh), 3),
            win_rate_pct=round(w_rate, 1),
            avg_position_size=0.0,
        ))

    return sorted(results, key=lambda r: -r.sharpe)


# ── 인자 분해 ────────────────────────────────────────────────────────────────

def factor_decomposition(
    portfolio_returns: pd.Series,
    factor_returns: dict[str, pd.Series],
) -> dict:
    """
    포트폴리오 수익률을 팩터 수익률로 OLS 회귀.
    팩터: SPY(시장), MOM(모멘텀), VOL(변동성 역수) 등

    반환: {factor_name: beta, "alpha": alpha, "r_squared": r2}
    """
    try:
        from numpy.linalg import lstsq

        # 공통 인덱스
        idx = portfolio_returns.index
        for f in factor_returns.values():
            idx = idx.intersection(f.index)
        if len(idx) < 20:
            return {"error": "insufficient_data"}

        y = portfolio_returns.loc[idx].values
        X_cols = []
        names = []
        for name, series in factor_returns.items():
            X_cols.append(series.loc[idx].values)
            names.append(name)

        X = np.column_stack([np.ones(len(y))] + X_cols)
        coeff, _, _, _ = lstsq(X, y, rcond=None)

        alpha = float(coeff[0])
        betas = {names[i]: round(float(coeff[i + 1]), 4) for i in range(len(names))}

        y_hat = X @ coeff
        ss_res = np.sum((y - y_hat) ** 2)
        ss_tot = np.sum((y - y.mean()) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

        return {"alpha": round(alpha * 252, 6), "r_squared": round(float(r2), 4), **betas}
    except Exception as e:
        logger.warning("factor_decomposition 오류: %s", e)
        return {"error": str(e)}


# ── 중복 전략 제거 권고 ───────────────────────────────────────────────────────

@dataclass
class PruningRecommendation:
    keep: list[str]
    remove: list[str]
    reason: dict[str, str]


def prune_redundant_strategies(
    contributions: list[StrategyContribution],
    corr_threshold: float = 0.80,
    min_win_rate: float = 0.45,
    min_strength: float = 0.10,
) -> PruningRecommendation:
    """
    중복·불안정 전략 제거 권고.

    제거 조건:
    - win_rate < min_win_rate (불안정 알파)
    - redundancy_score > corr_threshold (중복)
    - avg_signal_strength < min_strength (신호 강도 부족)
    """
    keep, remove, reason = [], [], {}

    # 성과 기준 정렬 (win_rate 우선)
    ranked = sorted(contributions, key=lambda c: -c.win_rate)
    seen_corr_groups: list[str] = []

    for c in ranked:
        reasons = []
        if c.win_rate < min_win_rate:
            reasons.append(f"win_rate={c.win_rate:.2f} < {min_win_rate}")
        if c.avg_signal_strength < min_strength:
            reasons.append(f"strength={c.avg_signal_strength:.3f} < {min_strength}")
        if c.name in seen_corr_groups:
            reasons.append(f"redundant (corr > {corr_threshold})")
        elif c.redundancy_score > corr_threshold:
            seen_corr_groups.append(c.name)

        if reasons:
            remove.append(c.name)
            reason[c.name] = "; ".join(reasons)
        else:
            keep.append(c.name)

    return PruningRecommendation(keep=keep, remove=remove, reason=reason)


# ── 민감도 분석 ──────────────────────────────────────────────────────────────

def sensitivity_analysis(
    run_fn,           # callable(params) → {"sharpe": float, "mdd_pct": float}
    base_params: dict,
    param_ranges: dict,  # {"fast": [10, 20, 50], "slow": [100, 150, 200]}
    target_metric: str = "sharpe",
) -> dict:
    """
    파라미터 한 개씩 변화시킬 때 target_metric 변화 측정.
    반환: {param_name: {value: metric_value, ...}, ...}
    """
    results = {}
    for param_name, values in param_ranges.items():
        param_results = {}
        for val in values:
            params = {**base_params, param_name: val}
            try:
                out = run_fn(params)
                param_results[val] = round(float(out.get(target_metric, 0)), 4)
            except Exception as e:
                logger.warning("sensitivity %s=%s failed: %s", param_name, val, e)
                param_results[val] = None
        results[param_name] = param_results

    # 각 파라미터의 metric 범위 (최대-최소) → 민감도 순위
    sensitivity = {}
    for pname, pres in results.items():
        vals = [v for v in pres.values() if v is not None]
        if vals:
            sensitivity[pname] = round(max(vals) - min(vals), 4)

    return {
        "per_param": results,
        "sensitivity_range": dict(sorted(sensitivity.items(), key=lambda x: -x[1])),
    }


# ── 통합 분석 리포트 ──────────────────────────────────────────────────────────

def portfolio_report(
    equity: pd.Series,
    trades: list[dict],
    price_histories: dict[str, pd.Series] = None,
    regime_labels: pd.Series = None,
    rolling_window: int = 63,
) -> dict:
    """
    포트폴리오 전체 분석 리포트.
    API /api/analysis 에서 직접 반환 가능한 dict.
    """
    from backend.quant.backtest.metrics import (
        cagr, max_drawdown, sharpe as sharpe_fn, sortino, calmar,
        win_rate, profit_factor
    )

    if equity.empty:
        return {"error": "no_equity_data"}

    roll_sh = rolling_sharpe(equity, rolling_window)
    roll_dd = rolling_drawdown(equity, rolling_window)

    report = {
        "summary": {
            "total_return_pct": round((equity.iloc[-1] / equity.iloc[0] - 1) * 100, 2),
            "cagr_pct": round(cagr(equity) * 100, 2),
            "sharpe": round(sharpe_fn(equity), 3),
            "sortino": round(sortino(equity), 3),
            "calmar": round(calmar(equity), 3),
            "max_drawdown_pct": round(max_drawdown(equity) * 100, 2),
            "win_rate_pct": round(win_rate(trades) * 100, 1),
            "profit_factor": round(profit_factor(trades), 3),
            "total_trades": len(trades),
        },
        "rolling": {
            "sharpe_current": round(float(roll_sh.iloc[-1]), 3) if not roll_sh.empty else None,
            "sharpe_min": round(float(roll_sh.min()), 3) if not roll_sh.empty else None,
            "drawdown_current_pct": round(float(roll_dd.iloc[-1]) * 100, 2) if not roll_dd.empty else None,
            "drawdown_worst_pct": round(float(roll_dd.min()) * 100, 2) if not roll_dd.empty else None,
        },
    }

    if regime_labels is not None:
        report["regime_breakdown"] = [
            vars(r) for r in regime_performance_breakdown(equity, regime_labels)
        ]

    if price_histories:
        from backend.quant.risk.engine import correlation_matrix, redundant_pairs
        rets_df = {sym: pd.DataFrame({"Close": s}) if isinstance(s, pd.Series) else s
                   for sym, s in price_histories.items()}
        corr = correlation_matrix(rets_df)
        if not corr.empty:
            report["correlation_matrix"] = corr.round(3).to_dict()
            report["redundant_pairs"] = redundant_pairs(corr, 0.80)

    return report
