"""Composable mandatory and informational health checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class HealthCheck:
    name: str
    passed: bool
    detail: str = ""
    mandatory: bool = True


@dataclass(frozen=True)
class HealthReport:
    checks: tuple[HealthCheck, ...]

    @property
    def healthy(self) -> bool:
        mandatory = tuple(check for check in self.checks if check.mandatory)
        return bool(mandatory) and all(check.passed for check in mandatory)


def combine_health_reports(*reports: HealthReport) -> HealthReport:
    return HealthReport(
        checks=tuple(check for report in reports for check in report.checks)
    )


def evaluate_runtime_health(
    observations: Iterable[object],
    *,
    expected_profiles: Iterable[str],
    identity_required: bool = True,
) -> HealthReport:
    by_profile = {
        str(getattr(observation, "profile")): observation
        for observation in observations
    }
    checks = []
    for profile in expected_profiles:
        observation = by_profile.get(profile)
        checks.append(
            HealthCheck(
                name=f"runtime:{profile}",
                passed=bool(
                    observation is not None and getattr(observation, "healthy", False)
                ),
                detail=(
                    (
                        "runtime identity agrees"
                        if identity_required
                        else "legacy runtime process and service ownership agree"
                    )
                    if observation is not None
                    and getattr(observation, "healthy", False)
                    else "runtime missing or identity mismatch"
                ),
            )
        )
    return HealthReport(checks=tuple(checks))
