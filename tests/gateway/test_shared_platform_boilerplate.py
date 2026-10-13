"""Invariants for the shared helper functions in ``gateway/platforms/_shared.py``.

Contract under test: the secret/precedence readers go through the ONE profile-scoped reader
(never an ambient ``os.environ`` read) — explicit scoped env → the profile's YAML → default —
and the external-fallback rung is honoured only before a scope is installed.
"""

from __future__ import annotations

import pytest

import agent.secret_scope as ss
import gateway.platforms._shared as shared


@pytest.fixture(autouse=True)
def _multiplex_off():
    prev = ss._MULTIPLEX_ACTIVE
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(prev)


def test_home_channel_name_defaults_to_home_everywhere(monkeypatch):
    """One rule for ``home_channel.name`` across adapters: ``<HOME_ENV>_NAME`` else the literal ``Home``
    (irc/ntfy/buzz previously used the chat id as its own name)."""
    monkeypatch.setattr(shared, "get_scoped_secret", lambda n, d=None, **k: {"X_HOME": "room"}.get(n, d))
    seed = shared.seed_extra_from_env((), home_env="X_HOME")
    assert seed["home_channel"] == {"chat_id": "room", "name": "Home"}
    monkeypatch.setattr(shared, "get_scoped_secret", lambda n, d=None, **k: {"X_HOME": "room", "X_HOME_NAME": "Ops"}.get(n, d))
    assert shared.seed_extra_from_env((), home_env="X_HOME")["home_channel"]["name"] == "Ops"
    assert shared.seed_extra_from_env((), home_env="X_HOME", home_default="fallback")["home_channel"]["chat_id"] == "room"
    monkeypatch.setattr(shared, "get_scoped_secret", lambda n, d=None, **k: d)
    assert shared.seed_extra_from_env((), home_env="X_HOME", home_default="fallback")["home_channel"]["chat_id"] == "fallback"
    assert "home_channel" not in shared.seed_extra_from_env((), home_env="X_HOME")


def test_extra_or_secret_precedence_env_then_yaml_then_default(monkeypatch):
    """Explicit scoped env → the profile's YAML → default; a blank env value is unset (#108440, #109032)."""
    env: dict = {}
    monkeypatch.setattr(shared, "get_scoped_secret", lambda n, d=None, **k: env.get(n, d))
    # YAML alone: explicit False is a real value; blank/None fall to the default.
    assert shared.extra_or_secret({"require_mention": False}, "require_mention", "X", "true") is False
    assert shared.extra_or_secret({"require_mention": ""}, "require_mention", "X", "true") == "true"
    assert shared.extra_or_secret(None, "require_mention", "X", "true") == "true"
    # Readers where a blank YAML value means "cleared" (channel whitelists) keep it as a value.
    assert shared.extra_or_secret({"allowed_channels": ""}, "allowed_channels", "X", "dflt", blank_is_unset=False) == ""
    assert shared.extra_or_secret({}, "allowed_channels", "X", "dflt", blank_is_unset=False) == "dflt"
    # An explicit env value beats YAML in either direction; a blank env value does not.
    env["X"] = "false"
    assert shared.extra_or_secret({"reactions": True}, "reactions", "X", "true") == "false"
    env["X"] = "true"
    assert shared.extra_or_secret({"reactions": False}, "reactions", "X", "false") == "true"
    env["X"] = "  "
    assert shared.extra_or_secret({"reactions": False}, "reactions", "X", "true") is False


def test_extra_or_secret_scoped_miss_never_reads_launch_env(monkeypatch):
    """Under a secondary's scope the launch process's os.environ is another profile's value: a miss
    falls to the secondary's OWN YAML, then the default — never to os.environ."""
    monkeypatch.setenv("X_FLAG", "launch-value")
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        assert shared.extra_or_secret({"flag": "yaml-value"}, "flag", "X_FLAG", "dflt") == "yaml-value"
        assert shared.extra_or_secret({}, "flag", "X_FLAG", "dflt") == "dflt"
    finally:
        ss.reset_secret_scope(token)
    # Unscoped (single-profile / default profile): env-over-YAML exactly as documented.
    ss.set_multiplex_active(False)
    assert shared.extra_or_secret({"flag": "yaml-value"}, "flag", "X_FLAG", "dflt") == "launch-value"


def test_external_fallback_consults_profile_scope_only_when_unscoped(monkeypatch):
    """Buzz's startup gate rung: a Bitwarden-managed key is visible through ``external_fallback`` before any
    scope exists, but never overrides an installed (authoritative) scope."""
    monkeypatch.delenv("K", raising=False)
    monkeypatch.setattr(shared, "_UNSCOPED_PROFILE_SECRETS", {"K": "external"})
    assert shared.get_scoped_secret("K") is None
    assert shared.get_scoped_secret("K", external_fallback=True) == "external"
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({"OTHER": "x"})
    try:
        assert shared.get_scoped_secret("K", "dflt", external_fallback=True) == "dflt"
    finally:
        ss.reset_secret_scope(token)
