from unittest.mock import AsyncMock

import pytest
from archie_agent.exec.tools import get_all_tools, mail
from archie_agent.exec.tools.google_common import (
    GoogleAuthorizationError,
    GoogleTransportError,
    GoogleValidationError,
)
from archie_shared.tool_policy import reset_policy_snapshot, set_policy_snapshot


async def test_mail_surface_uses_bulk_mutation_names():
    tools = get_all_tools()
    expected = {
        "mail.search",
        "mail.read",
        "mail.list_labels",
        "mail.label",
        "mail.archive",
        "mail.unarchive",
        "mail.mark_read",
        "mail.mark_unread",
        "mail.star",
        "mail.unstar",
    }
    assert expected <= tools.keys()
    assert "mail.modify_labels" not in tools


async def test_mail_read_write_gates(monkeypatch):
    calls = AsyncMock(return_value=[{"id": "m1", "subject": "Hi", "labelIds": ["INBOX"]}])
    monkeypatch.setattr(mail, "gmail_request", calls)
    monkeypatch.setattr(mail, "credential", lambda: type("C", (), {"access_token": "t"})())
    assert (await mail.search("hi"))[0]["id"] == "m1"
    token = set_policy_snapshot({"google": {"read": {"enabled": False}}})
    try:
        with pytest.raises(GoogleAuthorizationError):
            await mail.search("hi")
    finally:
        reset_policy_snapshot(token)


async def test_archive_uses_one_batch_request(monkeypatch):
    monkeypatch.setattr(mail, "credential", lambda: type("C", (), {"access_token": "t"})())
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    token = set_policy_snapshot({"google": {"write": {"enabled": True}}})
    try:
        result = await mail.archive(["m1", "m2"])
    finally:
        reset_policy_snapshot(token)
    assert result == {"count": 2, "complete": True}
    assert calls.await_args.args == (
        "batch_modify",
        {"ids": ["m1", "m2"], "addLabelIds": [], "removeLabelIds": ["INBOX"]},
        "t",
    )


async def test_system_operations_use_fixed_batch_labels(monkeypatch):
    monkeypatch.setattr(mail, "credential", lambda: type("C", (), {"access_token": "t"})())
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    token = set_policy_snapshot({"google": {"write": {"enabled": True}}})
    try:
        await mail.unarchive(["m1"])
        await mail.mark_read(["m1"])
        await mail.mark_unread(["m1"])
        await mail.star(["m1"])
        await mail.unstar(["m1"])
    finally:
        reset_policy_snapshot(token)
    payloads = [call.args[1] for call in calls.await_args_list]
    assert payloads == [
        {"ids": ["m1"], "addLabelIds": ["INBOX"], "removeLabelIds": []},
        {"ids": ["m1"], "addLabelIds": [], "removeLabelIds": ["UNREAD"]},
        {"ids": ["m1"], "addLabelIds": ["UNREAD"], "removeLabelIds": []},
        {"ids": ["m1"], "addLabelIds": ["STARRED"], "removeLabelIds": []},
        {"ids": ["m1"], "addLabelIds": [], "removeLabelIds": ["STARRED"]},
    ]


async def test_mutation_ids_validate_before_policy_or_provider(monkeypatch):
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    monkeypatch.setattr(mail, "require_write", lambda: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(GoogleValidationError):
        await mail.archive(["m1"] * 501)
    assert calls.await_count == 0


async def test_label_resolves_user_names_and_uses_one_batch(monkeypatch):
    monkeypatch.setattr(mail, "credential", lambda: type("C", (), {"access_token": "t"})())
    calls = AsyncMock(
        side_effect=[
            [{"id": "Label_1", "name": "Projects", "type": "USER"}],
            None,
        ]
    )
    monkeypatch.setattr(mail, "gmail_request", calls)
    monkeypatch.setattr(mail.cache, "get", lambda key: None)
    monkeypatch.setattr(mail.cache, "set", lambda *args: None)
    token = set_policy_snapshot({"google": {"read": {"enabled": True}, "write": {"enabled": True}}})
    try:
        result = await mail.label(["m1", "m2"], add=["Projects"])
    finally:
        reset_policy_snapshot(token)
    assert result == {"count": 2, "complete": True}
    assert calls.await_args_list[1].args[1] == {
        "ids": ["m1", "m2"],
        "addLabelIds": ["Label_1"],
        "removeLabelIds": [],
    }


async def test_label_rejects_system_labels_and_unknown_names(monkeypatch):
    monkeypatch.setattr(mail, "credential", lambda: type("C", (), {"access_token": "t"})())
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    monkeypatch.setattr(
        mail.cache,
        "get",
        lambda key: {"labels": [{"id": "INBOX", "name": "INBOX", "type": "SYSTEM"}]},
    )
    token = set_policy_snapshot({"google": {"read": {"enabled": True}, "write": {"enabled": True}}})
    try:
        with pytest.raises(GoogleValidationError):
            await mail.label(["m1"], add=["INBOX"])
        with pytest.raises(GoogleValidationError):
            await mail.label(["m1"], add=["Missing"])
    finally:
        reset_policy_snapshot(token)
    assert calls.await_count == 0


async def test_label_rejects_empty_or_duplicate_inputs(monkeypatch):
    with pytest.raises(GoogleValidationError):
        await mail.label(["m1"])
    with pytest.raises(GoogleValidationError):
        await mail.label(["m1", "m1"], add=["Projects"])


async def test_warm_label_cache_works_without_read_policy(monkeypatch):
    monkeypatch.setattr(mail, "credential", lambda: type("C", (), {"access_token": "t"})())
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    monkeypatch.setattr(
        mail.cache,
        "get",
        lambda key: {"labels": [{"id": "Label_1", "name": "Projects", "type": "USER"}]},
    )
    token = set_policy_snapshot(
        {"google": {"read": {"enabled": False}, "write": {"enabled": True}}}
    )
    try:
        result = await mail.label(["m1"], add=["Projects"])
    finally:
        reset_policy_snapshot(token)
    assert result == {"count": 1, "complete": True}
    assert calls.await_args.args[0] == "batch_modify"


async def test_malformed_label_cache_refreshes_before_resolution(monkeypatch):
    monkeypatch.setattr(mail, "credential", lambda: type("C", (), {"access_token": "t"})())
    calls = AsyncMock(
        side_effect=[
            [{"id": "Label_1", "name": "Projects", "type": "USER"}],
            None,
        ]
    )
    monkeypatch.setattr(mail, "gmail_request", calls)
    monkeypatch.setattr(
        mail.cache,
        "get",
        lambda key: {"labels": [{"id": "", "name": "Projects", "type": "USER"}]},
    )
    monkeypatch.setattr(mail.cache, "set", lambda *args: None)
    token = set_policy_snapshot({"google": {"read": {"enabled": True}, "write": {"enabled": True}}})
    try:
        await mail.label(["m1"], add=["Projects"])
    finally:
        reset_policy_snapshot(token)
    assert [call.args[0] for call in calls.await_args_list] == ["labels", "batch_modify"]


async def test_oversized_batch_rejects_before_write_policy(monkeypatch):
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    monkeypatch.setattr(mail, "require_write", lambda: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(GoogleValidationError):
        await mail.archive(["x" * 256] * 500)
    assert calls.await_count == 0


async def test_exact_500_ids_are_accepted(monkeypatch):
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    monkeypatch.setattr(mail, "credential", lambda: type("C", (), {"access_token": "t"})())
    token = set_policy_snapshot({"google": {"write": {"enabled": True}}})
    try:
        result = await mail.archive([f"m{i}" for i in range(500)])
    finally:
        reset_policy_snapshot(token)
    assert result == {"count": 500, "complete": True}
    assert calls.await_count == 1


async def test_batch_response_rejects_unexpected_payload(monkeypatch):
    async def fake_json(*args, **kwargs):
        return {"unexpected": True}

    monkeypatch.setattr(mail, "google_json", fake_json)
    with pytest.raises(GoogleTransportError):
        await mail.gmail_request(
            "batch_modify", {"ids": ["m1"], "addLabelIds": [], "removeLabelIds": []}, "t"
        )
