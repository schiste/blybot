"""Two-sided consent before anything is bridged (#81).

The property under test is that no admin can opt in a channel other than
their own: a scope joins a bridge only because an admin ran the command
*in that scope*, and its stored ``bridge_id`` is the record of that.
"""

from __future__ import annotations

from blybot.domain.models import ConsentMode, GroupProfile, Scope
from blybot.observability import Counters
from blybot.services import commands as c
from blybot.services.commands import CommandService
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy
from tests.fakes import InMemoryProfiles

TG = Scope("telegram", "-100500")
DC = Scope("discord", "900100")
IRC = Scope("irc", "#wikipedia-fr")


class _RecordingAnnouncer:
    def __init__(self) -> None:
        self.announced: list[tuple[list[Scope], str]] = []

    async def announce(self, scopes: list[Scope], text: str) -> None:
        self.announced.append((scopes, text))


def _service(
    store: InMemoryProfiles | None = None, *, announcer: object | None = None
) -> tuple[CommandService, InMemoryProfiles, _RecordingAnnouncer]:
    profiles = store if store is not None else InMemoryProfiles()
    heard = _RecordingAnnouncer()
    directory = ChannelDirectory(
        store=profiles,
        default_log_page="Project:Log",
        default_consent=ConsentMode.IMMEDIATE,
        default_repo="",
        page_suffix="logs",
    )
    service = CommandService(
        directory=directory,
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
        bridge_announcer=heard if announcer is None else announcer,  # type: ignore[arg-type]
    )
    return service, profiles, heard


async def _code_for(service: CommandService, scope: Scope) -> str:
    result = await service.bridge(scope, is_admin=True, tokens=["new"])
    assert result.ok, result.text
    return str(result.payload)


async def test_a_second_channel_joins_only_by_its_own_admin_acting_there() -> None:
    service, profiles, _heard = _service()
    code = await _code_for(service, DC)

    joined = await service.bridge(IRC, is_admin=True, tokens=["join", code])

    assert joined.ok
    assert profiles.profiles[DC].bridge_id == code
    assert profiles.profiles[IRC].bridge_id == code  # its own stored consent


async def test_a_non_admin_cannot_join_or_create_or_leave() -> None:
    service, profiles, _heard = _service()
    code = await _code_for(service, DC)

    for tokens in (["new"], ["join", code], ["leave"]):
        refused = await service.bridge(IRC, is_admin=False, tokens=tokens)
        assert refused.text == c.REPLY_NOT_ADMIN
    assert IRC not in profiles.profiles


async def test_joining_tells_the_channels_already_in_the_bridge() -> None:
    """Everyone being mirrored has to learn a new audience appeared."""
    service, _profiles, heard = _service()
    code = await _code_for(service, DC)

    await service.bridge(IRC, is_admin=True, tokens=["join", code])

    (scopes, text) = heard.announced[-1]
    assert scopes == [DC]
    assert "#wikipedia-fr (irc)" in text


async def test_leaving_is_unilateral_and_the_others_are_told() -> None:
    """A channel that stops being mirrored must not keep believing it is."""
    service, profiles, heard = _service()
    code = await _code_for(service, DC)
    await service.bridge(IRC, is_admin=True, tokens=["join", code])
    heard.announced.clear()

    left = await service.bridge(IRC, is_admin=True, tokens=["leave"])

    assert left.text == c.REPLY_BRIDGE_LEFT
    assert profiles.profiles[IRC].bridge_id is None
    (scopes, text) = heard.announced[-1]
    assert scopes == [DC]  # the departing channel is not told about itself
    assert "no longer relayed" in text


async def test_an_unknown_code_joins_nothing() -> None:
    service, profiles, _heard = _service()
    refused = await service.bridge(IRC, is_admin=True, tokens=["join", "not-a-code"])
    assert refused.text == c.REPLY_BRIDGE_UNKNOWN_CODE
    assert IRC not in profiles.profiles

    missing = await service.bridge(IRC, is_admin=True, tokens=["join"])
    assert missing.text == c.REPLY_BRIDGE_UNKNOWN_CODE


async def test_a_channel_cannot_be_in_two_bridges_at_once() -> None:
    """Two groups sharing a scope is what the set topology forbids (#77)."""
    service, _profiles, _heard = _service()
    first = await _code_for(service, DC)
    second = await _code_for(service, TG)

    clash = await service.bridge(DC, is_admin=True, tokens=["join", second])
    assert clash.text == c.REPLY_BRIDGE_ALREADY

    again = await service.bridge(DC, is_admin=True, tokens=["new"])
    assert again.text == c.REPLY_BRIDGE_ALREADY
    assert first != second


async def test_show_names_the_other_members_and_the_code() -> None:
    service, _profiles, _heard = _service()
    code = await _code_for(service, DC)
    await service.bridge(IRC, is_admin=True, tokens=["join", code])

    shown = await service.bridge(IRC, is_admin=False, tokens=["show"])  # open to anyone

    assert code in shown.text
    assert "900100 (discord)" in shown.text


async def test_show_reports_an_unbridged_or_solo_channel_honestly() -> None:
    """A bridge of one mirrors nothing; saying "bridged" would mislead."""
    service, _profiles, _heard = _service()
    assert (await service.bridge(IRC, is_admin=False, tokens=["show"])).text == (
        c.REPLY_BRIDGE_NOT_BRIDGED
    )
    await _code_for(service, DC)
    assert (await service.bridge(DC, is_admin=False, tokens=["show"])).text == (
        c.REPLY_BRIDGE_NOT_BRIDGED
    )


async def test_leaving_when_not_bridged_says_so() -> None:
    service, _profiles, _heard = _service()
    assert (await service.bridge(IRC, is_admin=True, tokens=["leave"])).text == (
        c.REPLY_BRIDGE_NOT_BRIDGED
    )


async def test_an_unknown_verb_is_answered_with_the_usage() -> None:
    service, _profiles, _heard = _service()
    for tokens in ([], ["wat"]):
        assert (await service.bridge(IRC, is_admin=True, tokens=tokens)).text == (
            c.REPLY_BRIDGE_USAGE
        )


async def test_bridging_refuses_where_nothing_could_relay() -> None:
    """Recording a consent nothing would honour is worse than refusing."""
    profiles = InMemoryProfiles()
    directory = ChannelDirectory(
        store=profiles,
        default_log_page="Project:Log",
        default_consent=ConsentMode.IMMEDIATE,
        default_repo="",
        page_suffix="logs",
    )
    service = CommandService(
        directory=directory,
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
    )  # no announcer: this deployment cannot bridge

    refused = await service.bridge(IRC, is_admin=True, tokens=["new"])

    assert refused.text == c.REPLY_BRIDGE_OFF_DEPLOY
    assert profiles.profiles == {}


async def test_a_channel_the_operator_does_not_serve_is_refused() -> None:
    service, _profiles, _heard = _service()
    service.groups = GroupPolicy(allowed={"-100999"})
    refused = await service.bridge(IRC, is_admin=True, tokens=["new"])
    assert refused.text == c.REPLY_NOT_ALLOWED


async def test_storage_failure_never_half_records_consent() -> None:
    store = InMemoryProfiles(profiles={DC: GroupProfile(scope=DC)})
    service, profiles, _heard = _service(store)
    profiles.fail = True

    result = await service.bridge(DC, is_admin=True, tokens=["new"])

    assert result.text == c.REPLY_STORAGE_DOWN
    assert result.ok is False


async def test_a_bridge_of_one_announces_to_nobody() -> None:
    """The first channel has no peers yet; there is no one to notify."""
    service, _profiles, heard = _service()
    await service.bridge(DC, is_admin=True, tokens=["new"])
    assert heard.announced == []


async def test_leaving_a_bridge_of_one_announces_to_nobody() -> None:
    """Nobody was being mirrored, so there is no one to tell."""
    service, profiles, heard = _service()
    await service.bridge(DC, is_admin=True, tokens=["new"])

    left = await service.bridge(DC, is_admin=True, tokens=["leave"])

    assert left.text == c.REPLY_BRIDGE_LEFT
    assert profiles.profiles[DC].bridge_id is None
    assert heard.announced == []
