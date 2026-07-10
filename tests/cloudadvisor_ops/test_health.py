from __future__ import annotations

from types import SimpleNamespace

from ops.cloudadvisor.hermes_ops.health import (
    HealthCheck,
    HealthReport,
    combine_health_reports,
    evaluate_runtime_health,
)


def test_runtime_health_requires_every_expected_profile_and_identity():
    observations = [
        SimpleNamespace(profile="default", healthy=True),
        SimpleNamespace(profile="tradingastrid", healthy=False),
    ]

    report = evaluate_runtime_health(
        observations,
        expected_profiles=["default", "tradingastrid", "tradingcio"],
    )

    checks = {check.name: check for check in report.checks}
    assert report.healthy is False
    assert checks["runtime:default"].passed is True
    assert checks["runtime:tradingastrid"].passed is False
    assert checks["runtime:tradingcio"].passed is False


def test_only_mandatory_health_failures_block_deployment():
    report = HealthReport(
        checks=(
            HealthCheck("required", True, mandatory=True),
            HealthCheck("informational", False, mandatory=False),
        )
    )
    blocking = HealthReport(checks=(HealthCheck("runtime", False, mandatory=True),))

    assert report.healthy is True
    assert combine_health_reports(report, blocking).healthy is False
