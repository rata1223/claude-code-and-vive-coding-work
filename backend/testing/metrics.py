"""
ValidationMetrics — TASK P3-01B 검증 지표 수집기.

페이퍼 트레이딩 검증 시나리오 전반에서 누적되는 운영 지표를 담는다.
PaperHarness 가 게이트/체결/복구 경로에서 직접 갱신하며, 시나리오 테스트와
docs/PAPER_TRADING_VALIDATION.md 의 결과 표가 이 값을 읽는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ValidationMetrics:
    successful_orders: int = 0          # 완전 체결(FILLED)된 주문 수
    rejected_orders: int = 0            # 브로커/게이트가 거부한 주문 수
    reconciliation_mismatches: int = 0  # reconciler 가 탐지한 갭 수
    duplicate_orders: int = 0           # pending 락으로 차단된 중복 주문
    duplicate_signals: int = 0          # 중복 신호로 차단된 진입
    stale_data_blocks: int = 0          # freshness 게이트가 차단한 주문
    corporate_action_events: int = 0    # 기업행위로 차단/처리된 건
    kill_switch_blocks: int = 0         # kill-switch HALTED 로 차단된 주문
    recovery_attempts: int = 0
    recovery_successes: int = 0
    restart_recovery_seconds: list[float] = field(default_factory=list)

    # ── 파생 지표 ────────────────────────────────────────────────────────────
    def recovery_success_rate(self) -> float:
        if self.recovery_attempts == 0:
            return 1.0
        return self.recovery_successes / self.recovery_attempts

    def avg_restart_recovery_time(self) -> float:
        if not self.restart_recovery_seconds:
            return 0.0
        return sum(self.restart_recovery_seconds) / len(self.restart_recovery_seconds)

    def record_recovery(self, ok: bool, seconds: float | None = None) -> None:
        self.recovery_attempts += 1
        if ok:
            self.recovery_successes += 1
        if seconds is not None:
            self.restart_recovery_seconds.append(seconds)

    def as_dict(self) -> dict:
        return {
            "successful_orders": self.successful_orders,
            "rejected_orders": self.rejected_orders,
            "reconciliation_mismatches": self.reconciliation_mismatches,
            "duplicate_orders": self.duplicate_orders,
            "duplicate_signals": self.duplicate_signals,
            "stale_data_blocks": self.stale_data_blocks,
            "corporate_action_events": self.corporate_action_events,
            "kill_switch_blocks": self.kill_switch_blocks,
            "recovery_attempts": self.recovery_attempts,
            "recovery_successes": self.recovery_successes,
            "recovery_success_rate": round(self.recovery_success_rate(), 4),
            "avg_restart_recovery_seconds": round(self.avg_restart_recovery_time(), 6),
        }
