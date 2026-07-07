"""Tests for ``default_resolver_health`` — liveness check for the `default`
resolver's gateway process (T3.4).

The `default` resolver runs as a worker spawned by the `default` profile's
gateway, so "is the `default` gateway alive" is the proxy used here. See
gateway/status.py:default_resolver_health for the disambiguation note.
"""

from types import SimpleNamespace

from gateway import status


def _fake_profile(name="default", path=None):
    return SimpleNamespace(name=name, path=path)


class TestDefaultResolverHealth:
    def test_resolver_up_when_gateway_running(self, tmp_path):
        profile = _fake_profile(path=tmp_path)

        result = status.default_resolver_health(
            profiles_fn=lambda: [profile],
            is_running_fn=lambda pid_path: True,
        )

        assert result["resolver"] == "default"
        assert result["alive"] is True
        assert result["reason"] == "running"
        assert result["pid_path"].endswith("gateway.pid")

    def test_resolver_down_when_gateway_not_running(self, tmp_path):
        profile = _fake_profile(path=tmp_path)

        result = status.default_resolver_health(
            profiles_fn=lambda: [profile],
            is_running_fn=lambda pid_path: False,
        )

        assert result["resolver"] == "default"
        assert result["alive"] is False
        assert result["reason"] == "gateway_not_running"
        assert result["pid_path"].endswith("gateway.pid")

    def test_profile_missing_does_not_call_is_running_fn(self):
        calls = []

        def _is_running(pid_path):
            calls.append(pid_path)
            return True

        result = status.default_resolver_health(
            profiles_fn=lambda: [],
            is_running_fn=_is_running,
        )

        assert result["resolver"] == "default"
        assert result["alive"] is False
        assert result["reason"] == "profile_missing"
        assert result["pid_path"] is None
        assert calls == []

    def test_profile_missing_when_only_other_profiles_exist(self):
        other = _fake_profile(name="scout", path=None)

        result = status.default_resolver_health(profiles_fn=lambda: [other])

        assert result["alive"] is False
        assert result["reason"] == "profile_missing"
        assert result["pid_path"] is None
