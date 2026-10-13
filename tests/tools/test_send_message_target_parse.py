"""Parser-only and lightweight routing tests for send_message targets.

These stay separate from ``test_send_message_tool.py`` because that module
skips wholesale when optional Telegram dependencies are not installed.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from tools.send_message_tool import _send_to_platform, send_message_tool
from tools.send_message_targets import _parse_target_ref


def _run_async_immediately(coro):
    return asyncio.run(coro)


def test_buzz_uuid_target_is_explicit() -> None:
    channel_id = "31b543d5-80d4-4df5-8a5c-cefca1a58fdd"

    assert _parse_target_ref("buzz", channel_id) == (channel_id, None, True)


def test_photon_e164_target_is_explicit() -> None:
    chat_id, thread_id, is_explicit = _parse_target_ref("photon", "+15551234567")

    assert chat_id == "+15551234567"
    assert thread_id is None
    assert is_explicit is True


def test_e164_target_still_requires_phone_platform() -> None:
    assert _parse_target_ref("matrix", "+15551234567")[2] is False


def test_resolved_opaque_plugin_target_uses_directory_id() -> None:
    from gateway.platform_registry import PlatformEntry, platform_registry

    platform_name = "opaque-resolved-test"
    entry = PlatformEntry(
        name=platform_name,
        label="Opaque resolved test",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
    )
    platform_registry.register(entry)
    platform = Platform(platform_name)
    pconfig = SimpleNamespace(enabled=True, token=None, extra={})
    config = SimpleNamespace(
        platforms={platform: pconfig},
        get_home_channel=lambda _platform: None,
    )
    try:
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch(
                 "gateway.channel_directory.resolve_channel_name",
                 return_value="opaque:directory-id",
             ), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch(
                 "tools.send_message_tool._send_to_platform",
                 new=AsyncMock(return_value={"success": True}),
             ) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": f"{platform_name}:Friendly name",
                        "message": "hello",
                    }
                )
            )
    finally:
        platform_registry.unregister(platform_name)

    assert result["success"] is True
    send_mock.assert_awaited_once_with(
        platform,
        pconfig,
        "opaque:directory-id",
        "hello",
        thread_id=None,
        media_files=[],
        force_document=False,
    )


def test_unresolved_plugin_target_requires_explicit_parser() -> None:
    from gateway.platform_registry import PlatformEntry, platform_registry

    platform_name = "opaque-verbatim-test"
    entry = PlatformEntry(
        name=platform_name,
        label="Opaque verbatim test",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
    )
    platform_registry.register(entry)
    platform = Platform(platform_name)
    # Simulate a fresh `hermes send` process: the dynamic Platform member
    # is known from config, but plugin discovery has not registered its
    # adapter entry yet.
    platform_registry.unregister(platform_name)
    pconfig = SimpleNamespace(enabled=True, token=None, extra={})
    config = SimpleNamespace(
        platforms={platform: pconfig},
        get_home_channel=lambda _platform: None,
    )
    try:
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch(
                 "gateway.channel_directory.resolve_channel_name",
                 return_value=None,
             ), \
             patch(
                 "hermes_cli.plugins.discover_plugins",
                 side_effect=lambda: platform_registry.register(entry),
             ) as discover_mock, \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch(
                 "tools.send_message_tool._send_to_platform",
                 new=AsyncMock(return_value={"success": True}),
             ) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": f"{platform_name}:dm:panyaozhen",
                        "message": "hello",
                    }
                )
            )
    finally:
        platform_registry.unregister(platform_name)

    assert set(result) == {"error"}
    discover_mock.assert_called_once_with()
    send_mock.assert_not_awaited()


def test_unresolved_builtin_target_keeps_directory_error() -> None:
    telegram_cfg = SimpleNamespace(enabled=True, token="***", extra={})
    config = SimpleNamespace(
        platforms={Platform.TELEGRAM: telegram_cfg},
        get_home_channel=lambda _platform: None,
    )

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("gateway.channel_directory.resolve_channel_name", return_value=None), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(return_value={"success": True}),
         ) as send_mock:
        result = json.loads(
            send_message_tool(
                {
                    "action": "send",
                    "target": "telegram:missing-room",
                    "message": "hello",
                }
            )
        )

    assert set(result) == {"error"}
    assert "missing-room" in result["error"]
    send_mock.assert_not_awaited()



def test_unresolved_builtin_target_passes_through_when_requested() -> None:
    """Cron and react keep the old pass-through behavior for unresolved
    built-in targets: with no model in the loop to react to an error, the
    raw id must reach the adapter, as it did before resolve_send_target
    took over these callers."""
    from tools.send_message_tool import resolve_send_target

    with patch("gateway.channel_directory.resolve_channel_name", return_value=None):
        chat_id, thread_id, error = resolve_send_target(
            "telegram", "ops-room", pass_unresolved_references=True
        )

    assert error is None
    assert chat_id == "ops-room"
    assert thread_id is None


def test_parserless_plugin_target_passes_through_when_requested() -> None:
    """A plugin platform that declares no parser has no explicit syntax at
    all, so passing the raw id through is the only way cron can target it."""
    from gateway.platform_registry import PlatformEntry, platform_registry
    from tools.send_message_tool import resolve_send_target

    platform_name = "opaque-cron-fallback-test"
    entry = PlatformEntry(
        name=platform_name,
        label="Opaque cron fallback test",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
    )
    platform_registry.register(entry)
    try:
        with patch("gateway.channel_directory.resolve_channel_name", return_value=None):
            chat_id, thread_id, error = resolve_send_target(
                platform_name, "dm:panyaozhen", pass_unresolved_references=True
            )
    finally:
        platform_registry.unregister(platform_name)

    assert error is None
    assert chat_id == "dm:panyaozhen"
    assert thread_id is None


def test_plugin_parser_stays_authoritative_despite_fallback() -> None:
    """A plugin that DOES declare a parser stays strict for every caller:
    its parser is the authority on native syntax, so an unrecognized
    target errors even with pass_unresolved_references."""
    from gateway.platform_registry import PlatformEntry, platform_registry
    from tools.send_message_tool import resolve_send_target

    platform_name = "opaque-parser-strict-test"
    entry = PlatformEntry(
        name=platform_name,
        label="Opaque parser strict test",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
        parse_target_ref_fn=lambda ref: None,
    )
    platform_registry.register(entry)
    try:
        with patch("gateway.channel_directory.resolve_channel_name", return_value=None):
            chat_id, _thread_id, error = resolve_send_target(
                platform_name, "dm:panyaozhen", pass_unresolved_references=True
            )
    finally:
        platform_registry.unregister(platform_name)

    assert chat_id is None
    assert error is not None


def test_unknown_platform_that_left_core_names_its_install_command(tmp_path, monkeypatch):
    """A platform that moved out of core into a catalog plugin, sent to while the plugin is absent."""
    from gateway.config import GatewayConfig
    from tools.send_message_targets import resolve_send_target
    from tools.send_message_tool import _resolve_platform_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _, _, _, err = _resolve_platform_config("homeassistant", GatewayConfig())
    assert "unregistered plugin platform: homeassistant" in err and "`hermes plugins install homeassistant`" in err
    _, _, err = resolve_send_target("homeassistant", "living room")
    assert "`hermes plugins install homeassistant`" in err
    _, _, _, err = _resolve_platform_config("nosuchplatform", GatewayConfig())
    assert err == "Unknown or unregistered plugin platform: nosuchplatform"
