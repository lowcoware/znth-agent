"""Tests for the /voice command and auto voice reply in the gateway."""

import importlib.util
import json
import os
import queue
import sys
import threading
import time
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def _ensure_discord_mock():
    """Install a lightweight discord mock when discord.py isn't available."""
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    discord_mod.opus = SimpleNamespace(is_loaded=lambda: True, load_opus=lambda *_args, **_kwargs: None)
    discord_mod.FFmpegPCMAudio = MagicMock
    discord_mod.PCMVolumeTransformer = MagicMock
    discord_mod.http = SimpleNamespace(Route=MagicMock)

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from gateway.platforms.base import SessionSource
from gateway.platforms.event import MessageEvent, MessageType


def _unbound_voice_adapter() -> AsyncMock:
    """A Discord adapter bound to text channel 123 with no join source (a programmatic join)."""
    from gateway.config import Platform
    adapter = AsyncMock()
    adapter._voice_text_channels, adapter._voice_sources = {111: 123}, {}
    adapter._client = MagicMock()
    adapter.handle_message = AsyncMock()
    adapter._thread_id_and_chat_for_channel = MagicMock(return_value=(None, "123"))
    adapter.build_source = MagicMock(side_effect=lambda **kw: SessionSource(platform=Platform.DISCORD, **kw))
    return adapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(text: str = "", message_type=MessageType.TEXT, chat_id="123") -> MessageEvent:
    source = SessionSource(
        chat_id=chat_id,
        user_id="user1",
        platform=MagicMock(),
    )
    source.platform.value = "telegram"
    source.thread_id = None
    event = MessageEvent(text=text, message_type=message_type, source=source)
    event.message_id = "msg42"
    return event


def _make_runner(tmp_path):
    """Create a bare GatewayRunner without calling __init__."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._VOICE_MODE_PATH = tmp_path / "gateway_voice_mode.json"
    runner._session_db = None
    runner.session_store = MagicMock()
    runner._is_user_authorized = lambda source: True
    return runner


# =====================================================================
# /voice command handler
# =====================================================================

class TestHandleVoiceCommand:

    @pytest.fixture
    def runner(self, tmp_path):
        return _make_runner(tmp_path)


    @pytest.mark.asyncio
    async def test_voice_off(self, runner):
        runner._voice_mode["telegram:123"] = "voice_only"
        event = _make_event("/voice off")
        await runner._handle_voice_command(event)
        assert runner._voice_mode["telegram:123"] == "off"


    @pytest.mark.asyncio
    async def test_toggle_on_to_off(self, runner):
        runner._voice_mode["telegram:123"] = "voice_only"
        event = _make_event("/voice")
        await runner._handle_voice_command(event)
        assert runner._voice_mode["telegram:123"] == "off"

    @pytest.mark.asyncio
    async def test_persistence_saved(self, runner):
        event = _make_event("/voice on")
        await runner._handle_voice_command(event)
        assert runner._VOICE_MODE_PATH.exists()
        data = json.loads(runner._VOICE_MODE_PATH.read_text())
        assert data["telegram:123"] == "voice_only"


    def test_sync_populates_enabled_chats_from_voice_modes(self, runner):
        """Issue #16007: sync also restores per-chat /voice on|tts opt-ins.

        The adapter's ``_auto_tts_enabled_chats`` must mirror chats whose
        persisted voice_mode is ``voice_only`` or ``all`` — without this,
        ``/voice on`` was relying on a "not in disabled set" default that
        silently enabled auto-TTS for every chat.
        """
        from gateway.config import Platform
        runner._voice_mode = {
            "telegram:off_chat": "off",
            "telegram:on_chat": "voice_only",
            "telegram:tts_chat": "all",
            "slack:999": "voice_only",  # wrong platform, must be ignored
        }
        adapter = SimpleNamespace(
            _auto_tts_default=False,
            _auto_tts_disabled_chats=set(),
            _auto_tts_enabled_chats=set(),
            platform=Platform.TELEGRAM,
        )

        runner._sync_voice_mode_state_to_adapter(adapter)

        assert adapter._auto_tts_disabled_chats == {"off_chat"}
        assert adapter._auto_tts_enabled_chats == {"on_chat", "tts_chat"}

    def test_sync_pushes_config_default_onto_adapter(self, runner, monkeypatch):
        """Issue #16007: ``voice.auto_tts`` must propagate to ``_auto_tts_default``."""
        from gateway.config import Platform

        fake_cfg = {"voice": {"auto_tts": True}}
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: fake_cfg,
        )
        adapter = SimpleNamespace(
            _auto_tts_default=False,
            _auto_tts_disabled_chats=set(),
            _auto_tts_enabled_chats=set(),
            platform=Platform.TELEGRAM,
        )

        runner._sync_voice_mode_state_to_adapter(adapter)

        assert adapter._auto_tts_default is True


    @pytest.mark.asyncio
    async def test_platform_isolation(self, runner):
        """Same chat_id on different platforms must not collide (#12542)."""
        telegram_event = _make_event("/voice on", chat_id="999")
        slack_event = _make_event("/voice off", chat_id="999")
        slack_event.source.platform.value = "slack"

        await runner._handle_voice_command(telegram_event)
        await runner._handle_voice_command(slack_event)

        assert runner._voice_mode["telegram:999"] == "voice_only"
        assert runner._voice_mode["slack:999"] == "off"


# =====================================================================
# Auto voice reply decision logic
# =====================================================================

class TestAutoVoiceReply:
    """Test the real _should_send_voice_reply method on GatewayRunner.

    The gateway has two TTS paths:
      1. base adapter auto-TTS: fires for voice input in _process_message_background
      2. gateway _send_voice_reply: fires based on voice_mode setting

    To prevent double audio, _send_voice_reply is skipped when voice input
    already triggered base adapter auto-TTS.

    For Discord voice channels, the base adapter now routes play_tts directly
    into VC playback, so the runner should still skip voice-input follow-ups to
    avoid double playback.
    """

    @pytest.fixture
    def runner(self, tmp_path):
        return _make_runner(tmp_path)

    def _call(self, runner, voice_mode, message_type, agent_messages=None,
              response="Hello!", in_voice_channel=False):
        """Call real _should_send_voice_reply on a GatewayRunner instance."""
        chat_id = "123"
        if voice_mode != "off":
            runner._voice_mode["telegram:" + chat_id] = voice_mode
        else:
            runner._voice_mode.pop("telegram:" + chat_id, None)

        event = _make_event(message_type=message_type)

        if in_voice_channel:
            mock_adapter = MagicMock()
            mock_adapter.is_in_voice_channel = MagicMock(return_value=True)
            event.raw_message = SimpleNamespace(guild_id=111, guild=None)
            runner.adapters[event.source.platform] = mock_adapter

        return runner._should_send_voice_reply(
            event, response, agent_messages or []
        )

    # -- Full platform x input x mode matrix --------------------------------
    #
    # Legend:
    #   base = base adapter auto-TTS (play_tts)
    #   runner = gateway _send_voice_reply
    #
    # | Platform      | Input | Mode       | base | runner | Expected     |
    # |---------------|-------|------------|------|--------|--------------|
    # | Telegram      | voice | off        | yes  | skip   | 1 audio      |
    # | Telegram      | voice | voice_only | yes  | skip*  | 1 audio      |
    # | Telegram      | voice | all        | yes  | skip*  | 1 audio      |
    # | Telegram      | text  | off        | skip | skip   | 0 audio      |
    # | Telegram      | text  | voice_only | skip | skip   | 0 audio      |
    # | Telegram      | text  | all        | skip | yes    | 1 audio      |
    # | Discord text  | voice | all        | yes  | skip*  | 1 audio      |
    # | Discord text  | text  | all        | skip | yes    | 1 audio      |
    # | Discord VC    | voice | all        | skip†| yes    | 1 audio (VC) |
    # | Web UI        | voice | off        | yes  | skip   | 1 audio      |
    # | Web UI        | voice | all        | yes  | skip*  | 1 audio      |
    # | Web UI        | text  | all        | skip | yes    | 1 audio      |
    # | Slack         | voice | all        | yes  | skip*  | 1 audio      |
    # | Slack         | text  | all        | skip | yes    | 1 audio      |
    #
    # * skip_double: voice input → base already handles
    # † Discord play_tts override skips when in VC

    # -- Telegram/Slack/Web: voice input, base handles ---------------------

    def test_voice_input_voice_only_skipped(self, runner):
        """voice_only + voice input: base auto-TTS handles it, runner skips."""
        assert self._call(runner, "voice_only", MessageType.VOICE) is False


    # -- Text input: only runner handles -----------------------------------

    def test_text_input_all_mode_runner_fires(self, runner):
        """all + text input: only runner fires (base auto-TTS only for voice)."""
        assert self._call(runner, "all", MessageType.TEXT) is True


    # -- Mode off: nothing fires -------------------------------------------

    def test_off_mode_voice(self, runner):
        assert self._call(runner, "off", MessageType.VOICE) is False


    # -- Discord VC exception: runner must handle --------------------------

    def test_discord_vc_voice_input_base_handles(self, runner):
        """Discord VC + voice input: base adapter play_tts plays in VC,
        so runner skips to avoid double playback."""
        assert self._call(runner, "all", MessageType.VOICE, in_voice_channel=True) is False


# =====================================================================
# _send_voice_reply
# =====================================================================

class TestSendVoiceReply:

    @pytest.fixture
    def runner(self, tmp_path):
        return _make_runner(tmp_path)

    @pytest.mark.asyncio
    async def test_calls_tts_and_send_voice(self, runner):
        from gateway.config import Platform

        mock_adapter = AsyncMock()
        mock_adapter.send_voice = AsyncMock()
        event = _make_event()
        event.source.platform = Platform.TELEGRAM
        runner.adapters[event.source.platform] = mock_adapter

        tts_result = json.dumps({"success": True, "file_path": "/tmp/test.ogg"})

        with patch("tools.tts_tool.text_to_speech_tool", return_value=tts_result) as mock_tts, \
             patch("tools.tts_text_normalize._strip_markdown_for_tts", side_effect=lambda t: t), \
             patch("os.path.isfile", return_value=True), \
             patch("os.unlink"), \
             patch("os.makedirs"):
            await runner._send_voice_reply(event, "Hello world")

        mock_adapter.send_voice.assert_called_once()
        assert mock_tts.call_args.kwargs["output_path"].endswith(".ogg")
        call_args = mock_adapter.send_voice.call_args
        assert call_args.kwargs.get("chat_id") == "123"


    @pytest.mark.asyncio
    async def test_auto_voice_reply_uses_thread_metadata_helper(self, runner):
        from gateway.config import Platform

        mock_adapter = AsyncMock()
        mock_adapter.send_voice = AsyncMock()
        event = _make_event()
        event.source.platform = Platform.TELEGRAM
        event.source.chat_type = "dm"
        event.source.thread_id = "20197"
        event.message_id = "462"
        runner.adapters[event.source.platform] = mock_adapter

        tts_result = json.dumps({"success": True, "file_path": "/tmp/test.ogg"})

        with patch("tools.tts_tool.text_to_speech_tool", return_value=tts_result), \
             patch("tools.tts_text_normalize._strip_markdown_for_tts", side_effect=lambda t: t), \
             patch("os.path.isfile", return_value=True), \
             patch("os.unlink"), \
             patch("os.makedirs"):
            await runner._send_voice_reply(event, "Hello world")

        mock_adapter.send_voice.assert_called_once()
        call_kwargs = mock_adapter.send_voice.call_args.kwargs
        assert call_kwargs["reply_to"] == "462"
        assert call_kwargs["metadata"] == {
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": "20197",
            "telegram_reply_to_message_id": "462",
            # Final voice reply is notify-worthy (issue #27970 Bug 2):
            # mirrors the final-text path in gateway/platforms/base.py.
            "notify": True,
        }


# =====================================================================
# Gateway voice channel commands (join / leave / input)
# =====================================================================

class TestVoiceChannelCommands:
    """Test _handle_voice_channel_join, _handle_voice_channel_leave,
    _handle_voice_channel_input on the GatewayRunner."""

    @pytest.fixture
    def runner(self, tmp_path):
        return _make_runner(tmp_path)

    def _make_discord_event(self, text="/voice channel", chat_id="123",
                            guild_id=111, user_id="user1"):
        """Create event with raw_message carrying guild info."""
        source = SessionSource(
            chat_id=chat_id,
            user_id=user_id,
            platform=MagicMock(),
        )
        source.platform.value = "discord"
        source.thread_id = None
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source)
        event.message_id = "msg42"
        event.raw_message = SimpleNamespace(guild_id=guild_id, guild=None)
        return event

    # -- _handle_voice_channel_join --


    @pytest.mark.asyncio
    async def test_join_success(self, runner):
        """Successful join sets voice_mode and returns confirmation."""
        mock_channel = MagicMock()
        mock_channel.name = "General"
        mock_adapter = AsyncMock()
        mock_adapter.join_voice_channel = AsyncMock(return_value=True)
        mock_adapter.get_user_voice_channel = AsyncMock(return_value=mock_channel)
        mock_adapter._voice_text_channels = {}
        mock_adapter._voice_sources = {}
        mock_adapter._voice_input_callback = None
        event = self._make_discord_event()
        event.source.chat_type = "group"
        event.source.chat_name = "Hermes Server / #general"
        runner.adapters[event.source.platform] = mock_adapter
        result = await runner._handle_voice_channel_join(event)
        assert "General" in result
        assert runner._voice_mode["discord:123"] == "all"
        assert mock_adapter._voice_sources[111]["chat_id"] == "123"
        assert mock_adapter._voice_sources[111]["chat_type"] == "group"


    @pytest.mark.asyncio
    async def test_join_missing_voice_dependencies(self, runner):
        """Missing PyNaCl/davey should return a user-actionable install hint."""
        mock_channel = MagicMock()
        mock_channel.name = "General"
        mock_adapter = AsyncMock()
        mock_adapter.join_voice_channel = AsyncMock(
            side_effect=RuntimeError("PyNaCl library needed in order to use voice")
        )
        mock_adapter.get_user_voice_channel = AsyncMock(return_value=mock_channel)
        event = self._make_discord_event()
        runner.adapters[event.source.platform] = mock_adapter

        result = await runner._handle_voice_channel_join(event)

        assert "PyNaCl" in result

    # -- _handle_voice_channel_leave --


    @pytest.mark.asyncio
    async def test_leave_success(self, runner):
        """Successful leave disconnects and clears voice mode."""
        mock_adapter = AsyncMock()
        mock_adapter.is_in_voice_channel = MagicMock(return_value=True)
        mock_adapter.leave_voice_channel = AsyncMock()
        event = self._make_discord_event("/voice leave")
        runner.adapters[event.source.platform] = mock_adapter
        runner._voice_mode["discord:123"] = "all"
        await runner._handle_voice_channel_leave(event)
        assert runner._voice_mode["discord:123"] == "off"
        mock_adapter.leave_voice_channel.assert_called_once_with(111)

    # -- _handle_voice_channel_input --


    @pytest.mark.asyncio
    async def test_input_creates_event_and_dispatches(self, runner):
        """Voice input creates synthetic event and calls handle_message."""
        from gateway.config import Platform
        mock_adapter = _unbound_voice_adapter()
        mock_channel = AsyncMock()
        mock_adapter._client.get_channel = MagicMock(return_value=mock_channel)
        runner.adapters[Platform.DISCORD] = mock_adapter
        await runner._handle_voice_channel_input(111, 42, "Hello from VC")
        mock_adapter.handle_message.assert_called_once()
        event = mock_adapter.handle_message.call_args[0][0]
        assert event.text == "Hello from VC"
        assert event.message_type == MessageType.VOICE
        assert event.source.chat_id == "123"
        # A typed message in a guild text channel is a "group" turn; voice keys the same session.
        assert (event.source.chat_type, event.source.guild_id) == ("group", "111")

    @pytest.mark.asyncio
    async def test_input_reroutes_speaker_without_changing_transport_owner(self, runner, monkeypatch):
        from gateway.config import Platform
        from gateway.profile_routing import parse_profile_routes

        runner.config = SimpleNamespace(
            multiplex_profiles=True,
            profile_routes=parse_profile_routes([
                {"name": "second", "platform": "discord", "bot_profile": "team-bot",
                 "user_id": "222", "profile": "second"},
            ]),
        )
        monkeypatch.setattr(
            "gateway.run._multiplex_profile_homes",
            lambda _config: [("team-bot", None), ("first", None), ("second", None)],
        )
        mock_adapter = AsyncMock()
        mock_adapter._owner_profile = "team-bot"
        mock_adapter._voice_text_channels = {111: 123}
        mock_adapter._voice_sources = {111: SessionSource(
            platform=Platform.DISCORD, chat_id="123", chat_type="channel",
            user_id="111", profile="first",
        ).to_dict()}
        mock_adapter._client = MagicMock()
        mock_adapter._client.get_channel = MagicMock(return_value=AsyncMock())
        mock_adapter.handle_message = AsyncMock()
        runner.adapters = {}
        runner._profile_adapters = {
            "team-bot": {Platform.DISCORD: mock_adapter},
            "second": {},
        }

        await runner._handle_voice_channel_input(111, 222, "Hello from VC", adapter=mock_adapter)

        source = mock_adapter.handle_message.call_args[0][0].source
        assert (source.user_id, source.profile) == ("222", "second")
        assert runner._transport_owner(source) == (mock_adapter, "team-bot")

    @pytest.mark.asyncio
    async def test_input_resolves_channel_prompt(self, runner):
        """Voice input must carry the bound text channel's channel_prompt (#50149)."""
        from gateway.config import Platform
        mock_adapter = _unbound_voice_adapter()
        mock_adapter._client.get_channel = MagicMock(return_value=AsyncMock())
        mock_adapter._resolve_channel_prompt = MagicMock(return_value="Be terse in #dev.")
        runner.adapters[Platform.DISCORD] = mock_adapter
        await runner._handle_voice_channel_input(111, 42, "Hello from VC")
        mock_adapter._resolve_channel_prompt.assert_called_once_with("123", None)
        event = mock_adapter.handle_message.call_args[0][0]
        assert event.channel_prompt == "Be terse in #dev."


    @pytest.mark.asyncio
    async def test_input_reuses_bound_source_metadata(self, runner):
        """Voice input should share the linked text channel session metadata."""
        from gateway.config import Platform

        bound_source = SessionSource(
            chat_id="123",
            chat_name="Hermes Server / #general",
            chat_type="group",
            user_id="user1",
            user_name="user1",
            platform=Platform.DISCORD,
        )

        mock_adapter = AsyncMock()
        mock_adapter._voice_text_channels = {111: 123}
        mock_adapter._voice_sources = {111: bound_source.to_dict()}
        mock_channel = AsyncMock()
        mock_adapter._client = MagicMock()
        mock_adapter._client.get_channel = MagicMock(return_value=mock_channel)
        mock_adapter.handle_message = AsyncMock()
        runner.adapters[Platform.DISCORD] = mock_adapter

        await runner._handle_voice_channel_input(111, 42, "Hello from VC")

        mock_adapter.handle_message.assert_called_once()
        event = mock_adapter.handle_message.call_args[0][0]
        assert event.source.chat_id == "123"
        assert event.source.chat_type == "group"
        assert event.source.chat_name == "Hermes Server / #general"
        assert event.source.user_id == "42"


    # -- _get_guild_id --

    def test_get_guild_id_from_guild(self, runner):
        event = _make_event()
        mock_guild = MagicMock()
        mock_guild.id = 555
        event.raw_message = SimpleNamespace(guild_id=None, guild=mock_guild)
        result = runner._get_guild_id(event)
        assert result == 555

    def test_get_guild_id_from_interaction(self, runner):
        event = _make_event()
        event.raw_message = SimpleNamespace(guild_id=777, guild=None)
        result = runner._get_guild_id(event)
        assert result == 777


# =====================================================================
# Callback wiring order (join)
# =====================================================================

class TestCallbackWiringOrder:
    """Verify callback is wired BEFORE join, not after."""


    @pytest.mark.asyncio
    async def test_join_failure_clears_callback(self, tmp_path):
        """If join fails with exception, callback is cleaned up."""
        runner = _make_runner(tmp_path)

        mock_channel = MagicMock()
        mock_channel.name = "General"
        mock_adapter = AsyncMock()
        mock_adapter.join_voice_channel = AsyncMock(
            side_effect=RuntimeError("No permission")
        )
        mock_adapter.get_user_voice_channel = AsyncMock(return_value=mock_channel)
        mock_adapter._voice_input_callback = None

        event = _make_event("/voice channel")
        event.raw_message = SimpleNamespace(guild_id=111, guild=None)
        runner.adapters[event.source.platform] = mock_adapter

        result = await runner._handle_voice_channel_join(event)
        assert "failed" in result.lower()
        assert mock_adapter._voice_input_callback is None


# =====================================================================
# Leave exception handling
# =====================================================================

class TestLeaveExceptionHandling:
    """Verify state is cleaned up even when leave_voice_channel raises."""

    @pytest.fixture
    def runner(self, tmp_path):
        return _make_runner(tmp_path)

    @pytest.mark.asyncio
    async def test_leave_exception_still_cleans_state(self, runner):
        """If leave_voice_channel raises, voice_mode is still cleaned up."""
        mock_adapter = AsyncMock()
        mock_adapter.is_in_voice_channel = MagicMock(return_value=True)
        mock_adapter.leave_voice_channel = AsyncMock(
            side_effect=RuntimeError("Connection reset")
        )
        mock_adapter._voice_input_callback = MagicMock()

        event = _make_event("/voice leave")
        event.raw_message = SimpleNamespace(guild_id=111, guild=None)
        runner.adapters[event.source.platform] = mock_adapter
        runner._voice_mode["telegram:123"] = "all"

        await runner._handle_voice_channel_leave(event)
        assert runner._voice_mode["telegram:123"] == "off"
        assert mock_adapter._voice_input_callback is None


class TestStreamTtsToSpeaker:
    """Functional tests for the streaming TTS pipeline."""

    def test_none_sentinel_flushes_buffer(self):
        """None sentinel causes remaining buffer to be spoken."""
        from tools.tts_tool_speaker import stream_tts_to_speaker
        text_q = queue.Queue()
        stop_evt = threading.Event()
        done_evt = threading.Event()
        spoken = []

        def display(text):
            spoken.append(text)

        text_q.put("Hello world.")
        text_q.put(None)

        stream_tts_to_speaker(text_q, stop_evt, done_evt, display_callback=display)
        assert done_evt.is_set()
        assert any("Hello" in s for s in spoken)

    def test_stop_event_aborts_early(self):
        """Setting stop_event causes early exit."""
        from tools.tts_tool_speaker import stream_tts_to_speaker
        text_q = queue.Queue()
        stop_evt = threading.Event()
        done_evt = threading.Event()
        spoken = []

        stop_evt.set()
        text_q.put("Should not be spoken.")
        text_q.put(None)

        stream_tts_to_speaker(text_q, stop_evt, done_evt, display_callback=lambda t: spoken.append(t))
        assert done_evt.is_set()
        assert len(spoken) == 0

    def test_done_event_set_on_exception(self):
        """tts_done_event is set even when an exception occurs."""
        from tools.tts_tool_speaker import stream_tts_to_speaker
        text_q = queue.Queue()
        stop_evt = threading.Event()
        done_evt = threading.Event()

        # Put a non-string that will cause concatenation to fail
        text_q.put(12345)
        text_q.put(None)

        stream_tts_to_speaker(text_q, stop_evt, done_evt)
        assert done_evt.is_set()


class TestVoiceTTSPlayback:
    """TTS playback: play_tts in VC, dedup, fallback."""

    # -- Runner dedup --

    @staticmethod
    def _make_runner():
        from gateway.run import GatewayRunner
        runner = object.__new__(GatewayRunner)
        runner._voice_mode = {}
        runner.adapters = {}
        return runner

    def _call_should_reply(self, runner, voice_mode, msg_type, response="Hello",
                           agent_msgs=None, already_sent=False):
        from gateway.platforms.base import SessionSource
        from gateway.platforms.event import MessageEvent
        from gateway.config import Platform
        runner._voice_mode["discord:ch1"] = voice_mode
        source = SessionSource(
            platform=Platform.DISCORD, chat_id="ch1",
            user_id="1", user_name="test", chat_type="channel",
        )
        event = MessageEvent(source=source, text="test", message_type=msg_type)
        return runner._should_send_voice_reply(
            event, response, agent_msgs or [], already_sent=already_sent,
        )

    # -- Streaming OFF (existing behavior, must not change) --

    def test_voice_input_runner_skips(self):
        """Streaming OFF + voice input: runner skips — base adapter handles."""
        from gateway.platforms.event import MessageType
        runner = self._make_runner()
        assert self._call_should_reply(runner, "all", MessageType.VOICE, already_sent=False) is False


    def test_error_response_no_tts(self):
        """Error response: no TTS regardless of voice_mode."""
        from gateway.platforms.event import MessageType
        runner = self._make_runner()
        assert self._call_should_reply(runner, "all", MessageType.TEXT, response="Error: boom") is False


    # -- Streaming ON (already_sent=True) --


    def test_streaming_on_agent_tts_dedup(self):
        """Streaming ON + agent called TTS: runner skips (dedup still works)."""
        from gateway.platforms.event import MessageType
        runner = self._make_runner()
        agent_msgs = [{"role": "assistant", "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "text_to_speech", "arguments": "{}"}}
        ]}]
        assert self._call_should_reply(
            runner, "all", MessageType.VOICE, agent_msgs=agent_msgs, already_sent=True,
        ) is False


# =====================================================================
# BasePlatformAdapter._should_auto_tts_for_chat — gate for auto-TTS
# on voice input. Regression test for Issue #16007.
# =====================================================================

class TestShouldAutoTtsForChat:
    """Three-layer gate: per-chat enable > per-chat disable > config default."""

    def _make_adapter(self, *, default: bool, enabled=(), disabled=()):
        """Build a bare adapter with only the attrs the gate reads."""
        adapter = SimpleNamespace(
            _auto_tts_default=default,
            _auto_tts_enabled_chats=set(enabled),
            _auto_tts_disabled_chats=set(disabled),
        )
        # Bind the unbound method — _should_auto_tts_for_chat only reads the
        # three attrs above via ``self.``, so an unbound call works.
        from gateway.platforms.base import BasePlatformAdapter
        return BasePlatformAdapter._should_auto_tts_for_chat, adapter

    def test_default_false_no_override_suppresses(self):
        """Issue #16007: voice.auto_tts=False and no per-chat state → no TTS."""
        fn, adapter = self._make_adapter(default=False)
        assert fn(adapter, "chat1") is False


    def test_explicit_enable_overrides_false_default(self):
        """``/voice on`` with config auto_tts=False still fires."""
        fn, adapter = self._make_adapter(default=False, enabled={"chat1"})
        assert fn(adapter, "chat1") is True


class TestStreamTtsTempfileFallback:
    """Regression for the temp-WAV fallback in stream_tts_to_speaker.

    When no sounddevice output stream is available the streaming path falls
    back to writing each sentence to a temp WAV and playing it via the system
    player.  ``wave.open()`` given a *file object* flushes but does NOT close
    it (it only closes files it opened itself, by name), so the OS handle to
    the temp file stays open.  On Windows that open write handle blocks the
    player from reading the file and blocks ``os.unlink()`` (WinError 32,
    silently swallowed), leaving orphaned temp .wav files behind.  The fix
    closes the handle before playback/cleanup; this test asserts the close
    happens before the play call.
    """

    def test_tempfile_handle_closed_before_playback(self, monkeypatch):
        import wave
        import tools.tts_tool as tts_mod
        import tools.voice_mode as vm
        from tools.tts_tool_speaker import stream_tts_to_speaker

        # Fake registry streamer so resolve_streaming_provider yields chunked
        # PCM regardless of which real providers are configured in the env.
        class _FakeStreamer:
            sample_rate = 24000
            channels = 1

            def stream(self, text):
                yield b"\x00\x00" * 240
                yield b"\x00\x00" * 240

        monkeypatch.setattr(
            "tools.tts_streaming.resolve_streaming_provider",
            lambda tts_config, preferred=None: _FakeStreamer(),
        )

        # Force sounddevice unavailable → output_stream is None → tempfile fallback.
        def _no_sounddevice():
            raise ImportError("sounddevice unavailable in test")
        monkeypatch.setattr(tts_mod, "_import_sounddevice", _no_sounddevice)

        events = []  # ordered log of ("close", name) / ("play", path)

        # Spy on NamedTemporaryFile to record when the handle is closed.
        real_ntf = tts_mod.tempfile.NamedTemporaryFile

        def _spy_ntf(*args, **kwargs):
            f = real_ntf(*args, **kwargs)
            orig_close = f.close

            def _tracked_close():
                events.append(("close", f.name))
                return orig_close()

            f.close = _tracked_close
            return f

        monkeypatch.setattr(tts_mod.tempfile, "NamedTemporaryFile", _spy_ntf)

        played = []

        def _fake_play(path):
            events.append(("play", path))
            played.append(path)
            # At play time the file must be a fully written, readable WAV.
            with wave.open(path, "rb") as wf:
                assert wf.getnframes() > 0

        monkeypatch.setattr(vm, "play_audio_file", _fake_play)

        text_q = queue.Queue()
        stop_evt = threading.Event()
        done_evt = threading.Event()
        text_q.put("This is a spoken sentence for the fallback. ")
        text_q.put(None)

        stream_tts_to_speaker(text_q, stop_evt, done_evt)

        assert done_evt.is_set()
        assert played, "temp-file fallback player was never invoked"

        play_idx = next(i for i, e in enumerate(events) if e[0] == "play")
        closes_before_play = [
            i for i, e in enumerate(events[:play_idx]) if e[0] == "close"
        ]
        assert closes_before_play, (
            "temp WAV handle must be closed BEFORE play_audio_file — an open "
            "write handle blocks playback and os.unlink() on Windows"
        )
        # And the temp file is cleaned up afterwards.
        assert not os.path.exists(played[0]), "temp WAV was not unlinked"
