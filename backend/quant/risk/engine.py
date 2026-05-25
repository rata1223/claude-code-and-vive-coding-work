"""
종합 리스크 엔진.

1. 트레일링 스탑 추적 (포지션별 peak price 관리)
2. 변동성 목표 포지션 스케일링
3. 상관관계 인식 노출 한도
4. 일일 손실 / MDD 킬스위치
5. 노출 상한 (심볼·전체 포트폴리오)
"""
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── 설정 ──────────────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    # 포지션 사이징
    target_vol_ann: float = 0.10          # 변동성 목표 연환산 10%
    max_position_pct: float = 0.05        # 종목당 최대 5%
    max_portfolio_exposure: float = 0.15  # 총 노출 15% (1.5M 기준 225K)

    # 트레일링 스탑
    trailing_stop_pct: float = 0.07       # 고점 대비 -7% 청산
    hard_stop_pct: float = 0.10           # 진입가 대비 -10% 하드스탑

    # 손실 한도
    daily_loss_limit_pct: float = 0.03    # 일일 3% 손실 → 당일 매수 차단
    weekly_loss_limit_pct: float = 0.06   # 주간 6% 손실 → 킬스위치
    mdd_limit_pct: float = 0.15           # MDD 15% → 전량 청산

    # 상관관계
    max_corr_overlap: float = 0.80        # 0.80 이상 상관 → 2번째 포지션 차단
    corr_window: int = 63                 # 상관계수 계산 기간 (영업일)

    # 변동성 스케일링
    vol_scale_floor: float = 0.3          # 스케일 최소값 (극단 변동성 시)
    vol_scale_cap: float = 1.5            # 스케일 최대값 (레버리지 방지)


# ── 트레일링 스탑 추적 ────────────────────────────────────────────────────────

@dataclass
class PositionStop:
    symbol: str
    entry_price: float
    entry_date: str
    peak_price: float          # 고점 (트레일링 스탑 기준)
    trailing_stop: float       # 현재 트레일링 스탑 가격
    hard_stop: float           # 하드 스탑 가격 (진입가 기반)
    qty: int

    def update_peak(self, current_price: float) -> None:
        if current_price > self.peak_price:
            self.peak_price = current_price
            self.trailing_stop = current_price * (1 - _config_trailing_pct)

    def is_stopped(self, current_price: float) -> tuple[bool, str]:
        if current_price <= self.hard_stop:
            return True, "hard_stop"
        if current_price <= self.trailing_stop:
            return True, "trailing_stop"
        return False, ""


# 전역 기본 트레일링 pct (PositionStop 클래스 내 참조용)
_config_trailing_pct = 0.07


class TrailingStopManager:
    """
    포지션별 트레일링 스탑 관리.

    사용 예:
        mgr = TrailingStopManager(RiskConfig())
        mgr.open("069500", qty=5, entry_price=10000)
        stops = mgr.check_stops({"069500": 9200})
        # stops = [("069500", "trailing_stop")]
    """

    def __init__(self, config: RiskConfig):
        self.config = config
        self._positions: dict[str, PositionStop] = {}
        global _config_trailing_pct
        _config_trailing_pct = config.trailing_stop_pct

    def open(self, symbol: str, qty: int, entry_price: float,
             entry_date: Optional[str] = None) -> None:
        ts = entry_price * (1 - self.config.trailing_stop_pct)
        hs = entry_price * (1 - self.config.hard_stop_pct)
        self._positions[symbol] = PositionStop(
            symbol=symbol,
            entry_price=entry_price,
            entry_date=entry_date or str(date.today()),
            peak_price=entry_price,
            trailing_stop=ts,
            hard_stop=hs,
            qty=qty,
        )
        logger.debug("TS 오픈 %s entry=%.2f ts=%.2f hs=%.2f", symbol, entry_price, ts, hs)

    def update(self, price_map: dict[str, float]) -> None:
        """현재가로 peak 및 트레일링 스탑 갱신."""
        for sym, pos in self._positions.items():
            price = price_map.get(sym)
            if price:
                pos.update_peak(price)

    def check_stops(self, price_map: dict[str, float]) -> list[tuple[str, str]]:
        """스탑 발동 종목 목록 반환. [(symbol, reason), ...]"""
        triggered = []
        for sym, pos in self._positions.items():
            price = price_map.get(sym)
            if price is None:
                continue
            stopped, reason = pos.is_stopped(price)
            if stopped:
                triggered.append((sym, reason))
                logger.warning("스탑 발동 %s @ %.2f (%s)", sym, price, reason)
        return triggered

    def close(self, symbol: str) -> Optional[PositionStop]:
        return self._positions.pop(symbol, None)

    def get_all(self) -> dict[str, PositionStop]:
        return dict(self._positions)


# ── 노출 및 상관관계 관리 ─────────────────────────────────────────────────────

class ExposureManager:
    """
    종목 간 상관관계 기반 노출 한도 관리.

    - 전체 포트폴리오 노출이 max_portfolio_exposure 초과 → 신규 매수 차단
    - 신규 진입 종목이 기존 종목과 max_corr_overlap 이상 상관 → 차단
    """

    def __init__(self, config: RiskConfig):
        self.config = config

    def can_add(
        self,
        candidate: str,
        existing_symbols: list[str],
        price_histories: dict[str, pd.DataFrame],
        current_exposure: float,
        capital: float,
    ) -> tuple[bool, str]:
        """
        신규 종목 추가 가능 여부.
        반환: (허용 여부, 이유)
        """
        # 총 노출 한도
        if capital > 0 and current_exposure / capital > self.config.max_portfolio_exposure:
            return False, f"포트폴리오 노출 한도 초과 ({current_exposure/capital:.1%})"

        # 상관관계 체크
        if candidate in price_histories and existing_symbols:
            corr = self._max_correlation(candidate, existing_symbols, price_histories)
            if corr > self.config.max_corr_overlap:
                return False, f"상관관계 과다 (r={corr:.2f} > {self.config.max_corr_overlap})"

        return True, "ok"

    def _max_correlation(
        self,
        candidate: str,
        existing: list[str],
        price_histories: dict[str, pd.DataFrame],
    ) -> float:
        """후보와 기존 포지션 간 최대 상관계수."""
        w = self.config.corr_window
        try:
            r_cand = (price_histories[candidate]["Close"]
                      .pct_change().dropna().iloc[-w:])
        except Exception:
            return 0.0

        max_corr = 0.0
        for sym in existing:
            if sym not in price_histories or sym == candidate:
                continue
            try:
                r_sym = (price_histories[sym]["Close"]
                         .pct_change().dropna().iloc[-w:])
                # 공통 인덱스 정렬
                common = r_cand.index.intersection(r_sym.index)
                if len(common) < 20:
                    continue
                corr = float(r_cand.loc[common].corr(r_sym.loc[common]))
                max_corr = max(max_corr, abs(corr))
            except Exception:
                continue
        return max_corr


# ── 킬스위치 + 손실 한도 ──────────────────────────────────────────────────────

@dataclass
class LossTracker:
    """일별·주별 손실 추적 + 킬스위치."""

    config: RiskConfig
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    peak_equity: float = 0.0
    current_equity: float = 0.0
    kill_switch: bool = False
    kill_reason: str = ""
    trade_date: date = field(default_factory=date.today)
    week_start: date = field(default_factory=date.today)

    def reset_daily(self) -> None:
        self.daily_pnl = 0.0
        self.trade_date = date.today()

    def reset_weekly(self) -> None:
        self.weekly_pnl = 0.0
        self.week_start = date.today()

    def record_pnl(self, pnl: float, current_equity: float) -> None:
        today = date.today()
        if today != self.trade_date:
            self.reset_daily()
        if (today - self.week_start).days >= 7:
            self.reset_weekly()

        self.daily_pnl += pnl
        self.weekly_pnl += pnl
        self.current_equity = current_equity
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        self._evaluate()

    def _evaluate(self) -> None:
        capital = max(self.peak_equity, 1.0)

        # 일일 손실 한도
        if self.daily_pnl / capital < -self.config.daily_loss_limit_pct:
            self.kill_switch = True
            self.kill_reason = (
                f"일일 손실 한도 초과 ({self.daily_pnl/capital:.2%})"
            )
            logger.error("킬스위치 [일일] %s", self.kill_reason)
            return

        # 주간 손실 한도
        if self.weekly_pnl / capital < -self.config.weekly_loss_limit_pct:
            self.kill_switch = True
            self.kill_reason = (
                f"주간 손실 한도 초과 ({self.weekly_pnl/capital:.2%})"
            )
            logger.error("킬스위치 [주간] %s", self.kill_reason)
            return

        # MDD 한도
        if self.peak_equity > 0:
            mdd = (self.current_equity - self.peak_equity) / self.peak_equity
            if mdd < -self.config.mdd_limit_pct:
                self.kill_switch = True
                self.kill_reason = f"MDD 한도 초과 ({mdd:.2%})"
                logger.error("킬스위치 [MDD] %s", self.kill_reason)

    def can_buy(self) -> tuple[bool, str]:
        if self.kill_switch:
            return False, self.kill_reason
        capital = max(self.peak_equity, 1.0)
        if self.daily_pnl / capital < -self.config.daily_loss_limit_pct * 0.8:
            return False, "일일 손실 80% — 예방적 매수 차단"
        return True, "ok"

    def manual_reset(self) -> None:
        self.kill_switch = False
        self.kill_reason = ""
        logger.info("킬스위치 수동 해제")


# ── 변동성 스케일링 ───────────────────────────────────────────────────────────

def vol_position_scale(
    df: pd.DataFrame,
    target_vol: float = 0.10,
    vol_window: int = 21,
    floor: float = 0.3,
    cap: float = 1.5,
) -> float:
    """
    실현변동성 대비 목표변동성 스케일 팩터 반환 (0.3 ~ 1.5).
    포지션 수량에 곱해 사용.
    """
    try:
        close = df["Close"]
        log_ret = np.log(close / close.shift(1)).dropna()
        realized = log_ret.iloc[-vol_window:].std() * np.sqrt(252)
        if realized <= 0:
            return 1.0
        return float(np.clip(target_vol / realized, floor, cap))
    except Exception:
        return 1.0


# ── 포트폴리오 상관관계 분석 ──────────────────────────────────────────────────

def correlation_matrix(price_histories: dict[str, pd.DataFrame],
                       window: int = 63) -> pd.DataFrame:
    """종목 간 수익률 상관행렬 반환."""
    rets = {}
    for sym, df in price_histories.items():
        try:
            r = df["Close"].pct_change().dropna().iloc[-window:]
            rets[sym] = r
        except Exception:
            pass
    if not rets:
        return pd.DataFrame()
    df_rets = pd.DataFrame(rets).dropna()
    return df_rets.corr()


def redundant_pairs(corr: pd.DataFrame, threshold: float = 0.80) -> list[tuple[str, str, float]]:
    """
    상관계수 threshold 이상인 전략/종목 쌍 반환.
    [(sym_a, sym_b, corr_value), ...]
    """
    pairs = []
    syms = list(corr.columns)
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            val = corr.loc[a, b]
            if abs(val) >= threshold:
                pairs.append((a, b, round(float(val), 4)))
    return sorted(pairs, key=lambda x: -abs(x[2]))
