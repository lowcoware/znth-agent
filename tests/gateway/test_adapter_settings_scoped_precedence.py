"""Per-profile adapter settings resolve explicit scoped env → the profile's own YAML → default.

Invariants from the #108440 post-merge review: a multiplexed secondary constructed under the real
``_profile_runtime_scope`` reads its own YAML flags (Telegram proxy, Telegram reactions), never
inherits the launch process's env on a scoped miss, and the central ``allow_bots`` gate agrees with
the adapter; an explicit env value still beats YAML for the owning profile (single-profile
contract). Real loader, real constructors; only transport is substituted.
"""

from __future__ import annotations

import asyncio
import contextlib
import types
from unittest.mock import patch

import pytest

from agent.secret_scope import set_multiplex_active
from gateway.config import Platform, load_gateway_config
from hermes_cli.plugins import discover_plugins


@pytest.fixture(autouse=True)
def _plugins():
    discover_plugins()


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """(launch_home, secondary_home); HERMES_HOME points at the launch home, multiplex off on exit."""
    launch = tmp_path / "launch"
    secondary = tmp_path / "launch" / "profiles" / "b2"
    secondary.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    yield launch, secondary
    set_multiplex_active(False)


@contextlib.contextmanager
def _secondary_scope(home):
    from gateway.run import _profile_runtime_scope
    set_multiplex_active(True)
    try:
        with _profile_runtime_scope(home, prepared_secret_scope={}):
            yield
    finally:
        set_multiplex_active(False)


def test_secondary_reads_own_yaml_and_never_the_launch_env(homes, monkeypatch):
    launch, secondary = homes
    (launch / "config.yaml").write_text(
        "telegram:\n  reactions: true\n", encoding="utf-8")
    load_gateway_config()  # launch profile bridges its YAML into os.environ (single-profile contract)
    (secondary / "config.yaml").write_text(
        "telegram:\n  enabled: true\n  proxy_url: http://127.0.0.1:18080\n", encoding="utf-8")
    from plugins.platforms.telegram import adapter as tg
    with _secondary_scope(secondary):
        cfg = load_gateway_config()
        t = tg.TelegramAdapter(cfg.platforms[Platform.TELEGRAM])
        # Omitted keys resolve to the DEFAULT, not the launch profile's bridged env.
        assert not t._reactions_enabled()
        # The secondary's YAML proxy reaches request construction without an env bridge.
        monkeypatch.setenv("HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", "true")
        built: list = []
        with patch.object(tg, "HTTPXRequest", lambda **kw: built.append(kw) or types.SimpleNamespace()), \
                patch.object(t, "_instrument_polling_request", side_effect=lambda r: r):
            asyncio.run(t._build_ptb_requests())
        assert [kw.get("proxy") for kw in built] == ["http://127.0.0.1:18080"] * 2


def test_explicit_env_beats_yaml_for_the_owning_profile(homes, monkeypatch):
    """Single-profile / owning-profile contract: TELEGRAM_REACTIONS=true beats the stock
    ``reactions: false`` (#109032)."""
    launch, _ = homes
    (launch / "config.yaml").write_text(
        "telegram:\n  reactions: false\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    from plugins.platforms.telegram.adapter import TelegramAdapter
    cfg = load_gateway_config()
    assert TelegramAdapter(cfg.platforms[Platform.TELEGRAM])._reactions_enabled() is True


def test_central_allow_bots_gate_honours_a_secondary_yaml_policy(homes):
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from plugins.platforms.telegram import adapter as tg
    _, secondary = homes
    (secondary / "config.yaml").write_text(
        "telegram:\n  enabled: true\n  allow_bots: all\n", encoding="utf-8")
    with _secondary_scope(secondary):
        cfg = load_gateway_config()
        t = tg.TelegramAdapter(cfg.platforms[Platform.TELEGRAM])
        runner = object.__new__(GatewayRunner)
        runner.config, runner._primary_profile_name, runner.adapters = cfg, "default", {}
        runner._profile_adapters = {"b2": {Platform.TELEGRAM: t}}
        src = SessionSource(platform=Platform.TELEGRAM, chat_id="C_TEST", chat_type="group",
                            user_id=None, is_bot=True, profile="b2")
        assert runner._is_user_authorized(src), Platform.TELEGRAM
