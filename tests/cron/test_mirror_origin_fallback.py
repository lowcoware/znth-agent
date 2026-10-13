"""Mirror eligibility for origin-fallback and explicit cron delivery targets.

Field report (enterprise, 2026-08-17): `cron.mirror_delivery: true` with
`deliver: origin` delivered the brief to the platform but never appended it to
the reply-facing gateway session, so a user reply hit a session with no context.

Root cause: the job was created by a provisioning script, so it carries no
captured origin. `deliver: origin` falls back to the home channel — the
user's actual conversation — but `_target_matches_origin` returns False for
an empty origin, so the mirror and the in_channel seed never fire. The June
origin-scoping refactor (c06ceb3232) correctly excluded broadcasts, but the
origin-FALLBACK target is not a broadcast: it is the best available stand-in
for the user's primary conversation.

Design under test:
- Delivery targets carry `_resolved_from` provenance used to determine mirror eligibility:
  * origin match            -> eligible (unchanged)
  * origin-fallback (deliver=origin, no origin) -> eligible (NEW)
  * explicit platform:chat  -> eligible ONLY with per-job attach_to_session
    (NEW, opt-in; the global flag never activates explicit targets)
  * user-written bare platform -> home conversation, same flags as origin-fallback
  * `all` broadcast expansion -> never eligible (unchanged invariant)
- Dedup across tokens (e.g. "origin,all" hitting the same chat) OR-merges
  eligibility so token order cannot strip it.
- The in_channel flat-session seed requires a DM-shaped target or a known
  user_id: group-channel session keys are user-isolated, and a seed without
  user_id would create an orphan session no reply ever resolves to.
"""

import pytest

from cron.scheduler import _deliver_result, _resolve_delivery_targets
from cron.scheduler_delivery import _target_mirror_eligible


@pytest.fixture(autouse=True)
def _home_channel(monkeypatch):
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "D0HOME")
    monkeypatch.delenv("DISCORD_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("MATRIX_HOME_ROOM", raising=False)


class TestMirrorEligibilityResolution:
    def test_origin_target_is_eligible(self):
        job = {
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "D0AAA", "chat_type": "dm"},
        }
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert _target_mirror_eligible(job, targets[0], global_mirror=True)

    def test_origin_fallback_target_is_eligible(self):
        """deliver=origin with no captured origin: the home-channel fallback
        is the user's conversation, not a broadcast — mirror it."""
        job = {"deliver": "origin", "origin": None}
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert targets[0]["chat_id"] == "D0HOME"
        assert _target_mirror_eligible(job, targets[0], global_mirror=True)

    def test_all_expansion_is_never_eligible(self):
        """Broadcast targets stay unmirrored even with the global flag on."""
        job = {"deliver": "all", "origin": None}
        targets = _resolve_delivery_targets(job)
        assert targets, "home channel should expand from 'all'"
        for t in targets:
            assert not _target_mirror_eligible(job, t, global_mirror=True)
            assert not _target_mirror_eligible(
                {**job, "attach_to_session": True}, t, global_mirror=True
            )

    def test_bare_platform_target_is_eligible_as_home(self):
        """A user-WRITTEN bare-platform token deliberately addresses the
        home channel — same conversation semantics as origin_fallback,
        so the same eligibility flags apply (field report 2026-09-02:
        managed ``deliver: telegram`` crons delivered but never injected).
        Broadcast expansions from ``all`` remain ineligible — see
        test_all_expansion_is_never_eligible."""
        job = {"deliver": "telegram", "origin": None}
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert _target_mirror_eligible(job, targets[0], global_mirror=True)
        assert not _target_mirror_eligible(job, targets[0], global_mirror=False)
        assert _target_mirror_eligible(
            {**job, "attach_to_session": True}, targets[0], global_mirror=False
        )
        assert not _target_mirror_eligible(
            {**job, "attach_to_session": False}, targets[0], global_mirror=True
        )

    @pytest.mark.parametrize("deliver", ["telegram,all", "all,telegram", " ALL , telegram "])
    def test_dedup_bare_and_all_keeps_home_eligibility(self, deliver):
        """An explicit home address stays continuable regardless of broadcast token order."""
        job = {"deliver": deliver, "origin": None}
        targets = _resolve_delivery_targets(job)
        home_targets = [t for t in targets if t["platform"].lower() == "telegram"]
        assert len(home_targets) == 1
        assert _target_mirror_eligible(job, home_targets[0], global_mirror=True)

    def test_explicit_target_not_eligible_under_global_flag(self):
        """Global mirror_delivery must not write sessions into arbitrary
        explicitly-addressed chats."""
        job = {"deliver": "telegram:D0EXPL", "origin": None}
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert not _target_mirror_eligible(job, targets[0], global_mirror=True)

    def test_explicit_target_eligible_with_per_job_attach(self):
        """attach_to_session=true on the job is the author declaring the
        explicit target a conversation — managed per-user DM crons."""
        job = {
            "deliver": "telegram:D0EXPL",
            "origin": None,
            "attach_to_session": True,
        }
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert _target_mirror_eligible(job, targets[0], global_mirror=False)

    def test_origin_and_all_dedup_keeps_eligibility(self):
        """'origin,all' resolving to the same home chat must not lose the
        fallback's eligibility to dedup order."""
        job = {"deliver": "origin,all", "origin": None}
        targets = _resolve_delivery_targets(job)
        # Home channel deduped to one target.
        home_targets = [t for t in targets if t["platform"].lower() == "telegram"]
        assert len(home_targets) == 1
        assert _target_mirror_eligible(job, home_targets[0], global_mirror=True)

    def test_all_and_origin_reversed_order_keeps_eligibility(self):
        job = {"deliver": "all,origin", "origin": None}
        targets = _resolve_delivery_targets(job)
        home_targets = [t for t in targets if t["platform"].lower() == "telegram"]
        assert len(home_targets) == 1
        assert _target_mirror_eligible(job, home_targets[0], global_mirror=True)

    def test_explicit_other_chat_with_origin_not_eligible(self):
        """An explicit target that is NOT the origin stays unmirrored under
        the global flag even when the job has an origin elsewhere."""
        job = {
            "deliver": "telegram:D0OTHER",
            "origin": {"platform": "telegram", "chat_id": "D0AAA", "chat_type": "dm"},
        }
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert not _target_mirror_eligible(job, targets[0], global_mirror=True)


class TestFallbackMirrorEndToEnd:
    """Drive _deliver_result with a stubbed sender + mirror recorder."""

    @pytest.fixture()
    def telegram_env(self, monkeypatch, tmp_path):
        home = tmp_path / "hermes-home"
        home.mkdir()
        (home / "config.yaml").write_text(
            "cron:\n  mirror_delivery: true\n"
            "platforms:\n  telegram:\n    enabled: true\n    token: '123456:TEST-TOKEN'\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))

        send_calls = []

        async def fake_sender(token, chat_id, message, *, media_files=None,
                              thread_id=None, force_document=False,
                              disable_link_previews=False):
            send_calls.append({"chat_id": chat_id, "thread_id": thread_id})
            return {"success": True, "chat_id": chat_id, "message_id": "1.2"}

        import hermes_cli.plugins as hp
        import tools.send_message_tool as smt

        # Out-of-process cron delivery for Telegram goes through the native
        # ``_send_telegram`` route rather than a plugin ``standalone_sender_fn``.
        monkeypatch.setattr(smt, "_send_telegram", fake_sender)
        monkeypatch.setattr(hp, "discover_plugins", lambda *a, **k: None)

        mirror_calls = []


        def fake_mirror(platform, chat_id, text, source_label="cli",
                        thread_id=None, user_id=None, role="assistant"):
            mirror_calls.append({
                "platform": platform, "chat_id": chat_id,
                "thread_id": thread_id, "user_id": user_id, "role": role,
            })
            return True

        import gateway.mirror as mirror_mod

        real_mirror = mirror_mod.mirror_to_session
        monkeypatch.setattr(mirror_mod, "mirror_to_session", fake_mirror)
        return {"send": send_calls, "mirror": mirror_calls, "real_mirror": real_mirror, "home": home}

    def test_origin_fallback_job_mirrors_brief(self, telegram_env):
        """The field repro: managed cron, deliver=origin, no origin captured.
        The brief must be mirrored into the home-channel session."""
        job = {"id": "j1", "name": "brief", "deliver": "origin", "origin": None}
        err = _deliver_result(job, "Risk-off close brief", adapters=None, loop=None)
        assert err is None
        assert len(telegram_env["send"]) == 1
        assert len(telegram_env["mirror"]) == 1, (
            "origin-fallback delivery must mirror the brief into the "
            "home-channel session (the reply-continuity bug)"
        )
        assert telegram_env["mirror"][0]["chat_id"] == "D0HOME"
        assert telegram_env["mirror"][0]["role"] == "user"

    def test_all_broadcast_does_not_mirror(self, telegram_env):
        job = {"id": "j2", "name": "cast", "deliver": "all", "origin": None}
        err = _deliver_result(job, "broadcast text", adapters=None, loop=None)
        assert err is None
        assert len(telegram_env["send"]) == 1
        assert len(telegram_env["mirror"]) == 0

    def test_managed_bare_platform_job_mirrors_brief(self, telegram_env, monkeypatch):
        """The managed-cron shape delivers into the session a home-channel reply resumes."""
        import gateway.mirror as mirror_mod
        from gateway.config import GatewayConfig, Platform
        from gateway.session import SessionSource, SessionStore

        monkeypatch.setattr(mirror_mod, "mirror_to_session", telegram_env["real_mirror"])
        store = SessionStore(telegram_env["home"] / "sessions", GatewayConfig())
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="D0HOME", chat_type="dm")
        session = store.get_or_create_session(source)
        job = {"id": "h1", "name": "managed", "deliver": "telegram", "origin": None}
        err = _deliver_result(job, "morning brief", adapters=None, loop=None)
        assert err is None
        assert len(telegram_env["send"]) == 1
        reply_session = store.get_or_create_session(source)
        assert reply_session.session_id == session.session_id
        messages = store.load_transcript(reply_session.session_id)
        assert [m["role"] for m in messages] == ["user"]
        assert "morning brief" in messages[0]["content"]

    def test_explicit_target_with_attach_mirrors(self, telegram_env):
        job = {
            "id": "j3", "name": "managed-dm", "deliver": "telegram:D0USER7",
            "origin": None, "attach_to_session": True,
        }
        err = _deliver_result(job, "managed brief", adapters=None, loop=None)
        assert err is None
        assert len(telegram_env["mirror"]) == 1
        assert telegram_env["mirror"][0]["chat_id"] == "D0USER7"

    def test_explicit_target_without_attach_does_not_mirror(self, telegram_env):
        job = {
            "id": "j4", "name": "plain-explicit", "deliver": "telegram:D0USER8",
            "origin": None,
        }
        err = _deliver_result(job, "plain text", adapters=None, loop=None)
        assert err is None
        assert len(telegram_env["mirror"]) == 0

    def test_origin_job_still_mirrors_unchanged(self, telegram_env):
        """Regression control: the June origin-scoped behavior is untouched."""
        job = {
            "id": "j5", "name": "origin-job", "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "D0AAA", "chat_type": "dm"},
        }
        err = _deliver_result(job, "origin brief", adapters=None, loop=None)
        assert err is None
        assert len(telegram_env["mirror"]) == 1
        assert telegram_env["mirror"][0]["chat_id"] == "D0AAA"


class TestInChannelSeedUserIdGuard:
    """Group-channel seeds are user-keyed; a seed with no user_id would create
    an orphan session. DM targets are safe (key has no user_id)."""

    def test_seed_requires_dm_or_user_id(self):
        from cron.scheduler_delivery import _inchannel_seed_allowed

        # DM-shaped chat, no user_id: allowed (DM keys don't embed user).
        assert _inchannel_seed_allowed(is_dm=True, user_id=None)
        # Group chat with known user: allowed.
        assert _inchannel_seed_allowed(is_dm=False, user_id="U123")
        # Group chat, no user: refused — would orphan the session.
        assert not _inchannel_seed_allowed(is_dm=False, user_id=None)
