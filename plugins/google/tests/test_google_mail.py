import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from archie_agent.exec.plugin import context, plugin_tools
from archie_plugins.google import mail
from archie_plugins.google.common import (
    GoogleAuthorizationError,
    GoogleTransportError,
    GoogleValidationError,
)


def _warm_labels(labels):
    """Store ``labels`` in the installed context's cache under the mail key."""
    context().cache.set("google.labels", {"labels": labels}, 60)


def test_message_body_is_returned_whole_after_metadata():
    import base64

    body = "b" * 100_000
    data = base64.urlsafe_b64encode(body.encode()).decode()
    raw = {"id": "m1", "payload": {"mimeType": "text/plain", "body": {"data": data}}}
    result = mail._message(raw)
    assert result["body"] == body
    assert list(result)[-1] == "body"


async def test_mail_surface_uses_bulk_mutation_names():
    tools = plugin_tools()
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


async def test_mail_read_write_gates(monkeypatch, policy):
    calls = AsyncMock(return_value=[{"id": "m1", "subject": "Hi", "labelIds": ["INBOX"]}])
    monkeypatch.setattr(mail, "gmail_request", calls)
    assert (await mail.search("hi"))[0]["id"] == "m1"
    policy({"google": {"read": {"enabled": False}}})
    with pytest.raises(GoogleAuthorizationError):
        await mail.search("hi")


async def test_archive_uses_one_batch_request(monkeypatch, policy):
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    policy({"google": {"write": {"enabled": True}}})
    result = await mail.archive(["m1", "m2"])
    assert result == {"count": 2, "complete": True}
    assert calls.await_args.args == (
        "batch_modify",
        {"ids": ["m1", "m2"], "addLabelIds": [], "removeLabelIds": ["INBOX"]},
        "t",
    )


async def test_system_operations_use_fixed_batch_labels(monkeypatch, policy):
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    policy({"google": {"write": {"enabled": True}}})
    await mail.unarchive(["m1"])
    await mail.mark_read(["m1"])
    await mail.mark_unread(["m1"])
    await mail.star(["m1"])
    await mail.unstar(["m1"])
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


async def test_label_resolves_user_names_and_uses_one_batch(monkeypatch, policy):
    calls = AsyncMock(
        side_effect=[
            [{"id": "Label_1", "name": "Projects", "type": "USER"}],
            None,
        ]
    )
    monkeypatch.setattr(mail, "gmail_request", calls)
    policy({"google": {"read": {"enabled": True}, "write": {"enabled": True}}})
    result = await mail.label(["m1", "m2"], add=["Projects"])
    assert result == {"count": 2, "complete": True}
    assert calls.await_args_list[1].args[1] == {
        "ids": ["m1", "m2"],
        "addLabelIds": ["Label_1"],
        "removeLabelIds": [],
    }


async def test_label_rejects_system_labels_and_unknown_names(monkeypatch, policy):
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    _warm_labels([{"id": "INBOX", "name": "INBOX", "type": "SYSTEM"}])
    policy({"google": {"read": {"enabled": True}, "write": {"enabled": True}}})
    with pytest.raises(GoogleValidationError):
        await mail.label(["m1"], add=["INBOX"])
    with pytest.raises(GoogleValidationError):
        await mail.label(["m1"], add=["Missing"])
    assert calls.await_count == 0


async def test_label_lookup_is_cached_in_the_plugin_cache(monkeypatch, google_context_installed):
    calls = AsyncMock(return_value=[{"id": "Label_1", "name": "Projects", "type": "USER"}])
    monkeypatch.setattr(mail, "gmail_request", calls)
    assert await mail.list_labels() == [{"name": "Projects", "type": "USER"}]
    assert await mail.list_labels() == [{"name": "Projects", "type": "USER"}]
    assert calls.await_count == 1
    cache = context().cache
    assert cache.root == google_context_installed / "cache" / "google"
    assert (cache.root / "google.labels.json").is_file()
    assert cache.get("google.labels") == {
        "labels": [{"id": "Label_1", "name": "Projects", "type": "USER"}]
    }
    ttl = datetime.fromisoformat(
        json.loads((cache.root / "google.labels.json").read_text())["expires"]
    ) - datetime.now(UTC)
    assert timedelta(hours=23) < ttl <= timedelta(hours=24)


async def test_label_rejects_empty_or_duplicate_inputs(monkeypatch):
    with pytest.raises(GoogleValidationError):
        await mail.label(["m1"])
    with pytest.raises(GoogleValidationError):
        await mail.label(["m1", "m1"], add=["Projects"])


async def test_warm_label_cache_works_without_read_policy(monkeypatch, policy):
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    _warm_labels([{"id": "Label_1", "name": "Projects", "type": "USER"}])
    policy({"google": {"read": {"enabled": False}, "write": {"enabled": True}}})
    result = await mail.label(["m1"], add=["Projects"])
    assert result == {"count": 1, "complete": True}
    assert calls.await_args.args[0] == "batch_modify"


async def test_malformed_label_cache_refreshes_before_resolution(monkeypatch, policy):
    calls = AsyncMock(
        side_effect=[
            [{"id": "Label_1", "name": "Projects", "type": "USER"}],
            None,
        ]
    )
    monkeypatch.setattr(mail, "gmail_request", calls)
    _warm_labels([{"id": "", "name": "Projects", "type": "USER"}])
    policy({"google": {"read": {"enabled": True}, "write": {"enabled": True}}})
    await mail.label(["m1"], add=["Projects"])
    assert [call.args[0] for call in calls.await_args_list] == ["labels", "batch_modify"]


async def test_oversized_batch_rejects_before_write_policy(monkeypatch):
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    monkeypatch.setattr(mail, "require_write", lambda: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(GoogleValidationError):
        await mail.archive(["x" * 256] * 500)
    assert calls.await_count == 0


async def test_exact_500_ids_are_accepted(monkeypatch, policy):
    calls = AsyncMock(return_value=None)
    monkeypatch.setattr(mail, "gmail_request", calls)
    policy({"google": {"write": {"enabled": True}}})
    result = await mail.archive([f"m{i}" for i in range(500)])
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
