"""incident.io timeline, update and alert tool tests with a fake client (no network)."""

import contextvars
import json
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from archie_agent.exec.envelope import Envelope
from archie_agent.exec.plugin import plugin_tools
from archie_agent.exec.runner import run
from archie_agent.exec.tools import get_all_tools
from archie_plugins.incident_io.alerts import get_alert, list_alerts
from archie_plugins.incident_io.client import IncidentIoInputError
from archie_plugins.incident_io.directory import (
    list_catalog_entries,
    list_catalog_types,
    list_teams,
    list_users,
)
from archie_plugins.incident_io.incidents import incident_timeline, incident_updates
from archie_plugins.incident_io.on_call import (
    list_escalation_paths,
    list_schedules,
    on_call,
)
from archie_plugins.incident_io.work import (
    get_postmortem,
    list_actions,
    list_follow_ups,
    list_postmortems,
)

PUBLIC = {
    "get_alert",
    "get_incident",
    "get_postmortem",
    "incident_timeline",
    "incident_updates",
    "list_actions",
    "list_alerts",
    "list_catalog_entries",
    "list_catalog_types",
    "list_escalation_paths",
    "list_follow_ups",
    "list_incidents",
    "list_postmortems",
    "list_schedules",
    "list_teams",
    "list_users",
    "on_call",
    "reference",
}


async def test_timeline_resolves_reference_to_ulid_and_normalises(fake):
    client = fake(
        {
            "/v2/incidents/42": {"incident": {"id": "01INC"}},
            "/v2/incident_timeline_items": {
                "incident_timeline_items": [
                    {
                        "id": "t1",
                        "incident_id": "01INC",
                        "timestamp": "2026-10-01T10:00:00Z",
                        "title": "Status changed",
                        "creator": {"api_key": {"id": "k1", "name": "Datadog"}},
                        "activity_log_id": "a1",
                    }
                ]
            },
        },
        "incidents",
    )
    result = await incident_timeline("INC-42", limit=10)
    assert client.calls[0][1] == "/v2/incidents/42"
    assert client.calls[1][2] == {"incident_id": "01INC"}
    assert result == [
        {
            "id": "t1",
            "timestamp": "2026-10-01T10:00:00Z",
            "title": "Status changed",
            "description": None,
            "creator": {"type": "api_key", "name": "Datadog"},
        }
    ]


async def test_updates_use_ulid_directly_and_normalise(fake):
    client = fake(
        {
            "/v2/incident_updates": {
                "incident_updates": [
                    {
                        "id": "u1",
                        "updater": {"user": {"id": "p1", "name": "Sam"}},
                        "new_incident_status": {"name": "Monitoring", "category": "live"},
                        "new_severity": {"name": "Minor"},
                        "message": "Fix deployed",
                        "created_at": "2026-10-01T12:00:00Z",
                    }
                ]
            }
        },
        "incidents",
    )
    result = await incident_updates("01INC")
    assert [call[1] for call in client.calls] == ["/v2/incident_updates"]
    assert result[0]["new_status"] == {"name": "Monitoring", "category": "live"}
    assert result[0]["new_severity"] == "Minor"
    assert result[0]["updater"]["name"] == "Sam"
    assert result[0]["message"] == "Fix deployed"


async def test_timeline_rejects_bad_limit_before_request(fake):
    client = fake({}, "incidents")
    with pytest.raises(IncidentIoInputError):
        await incident_timeline("INC-1", limit=0)
    assert client.calls == []


async def test_list_alerts_encodes_filters_and_caps_page_size(fake):
    client = fake(
        {
            "/v2/alerts": {
                "alerts": [
                    {
                        "id": "al1",
                        "title": "High 5xx",
                        "status": "firing",
                        "alert_source_id": "src1",
                        "tags": ["checkout", {"bad": 1}],
                        "attributes": [{"attribute": {"name": "service"}}],
                    }
                ]
            }
        },
        "alerts",
    )
    result = await list_alerts(
        status="firing", source=["src1"], created_after="2026-10-01T10:00:00Z", limit=80
    )
    call = client.calls[0]
    assert call[2] == {
        "status[one_of]": ["firing"],
        "alert_source[one_of]": ["src1"],
        "created_at[gte]": "2026-10-01T10:00:00Z",
    }
    assert call[3:] == (80, 50)
    assert result[0]["tags"] == ["checkout"]
    assert "attributes" not in result[0]


@pytest.mark.parametrize(
    "kwargs",
    [{"status": "open"}, {"source": "bad id!"}, {"created_after": "nope"}, {"limit": 101}],
)
async def test_list_alerts_rejects_invalid_input(fake, kwargs):
    client = fake({}, "alerts")
    with pytest.raises(IncidentIoInputError):
        await list_alerts(**kwargs)
    assert client.calls == []


async def test_get_alert_includes_attributes_and_incident_ids(fake):
    client = fake(
        {
            "/v2/alerts/al1": {
                "alert": {
                    "id": "al1",
                    "title": "High 5xx",
                    "description": "d" * 9000,
                    "status": "resolved",
                    "attributes": [
                        {
                            "attribute": {"name": "service"},
                            "value": {"literal": "svc-1", "label": "checkout"},
                        },
                        {
                            "attribute": {"name": "teams"},
                            "array_value": [{"label": "payments"}, {"literal": "core"}],
                        },
                    ],
                }
            },
            "/v2/incident_alerts": {
                "incident_alerts": [{"incident": {"id": "01INC"}}, {"incident": {"id": "01INC"}}]
            },
        },
        "alerts",
    )
    result = await get_alert("al1")
    assert client.calls[1][2] == {"alert_id": "al1"}
    assert result["attributes"] == [
        {"name": "service", "values": ["checkout"]},
        {"name": "teams", "values": ["payments", "core"]},
    ]
    assert result["incident_ids"] == ["01INC"]
    assert len(result["description"]) == 8000


async def test_get_alert_rejects_path_traversal(fake):
    client = fake({}, "alerts")
    with pytest.raises(IncidentIoInputError):
        await get_alert("../incidents")
    assert client.calls == []


USER = {"id": "u1", "name": "Sam", "email": "s@x.io", "slack_user_id": "U1", "role": "viewer"}


async def test_actions_filter_status_locally_and_resolve_incident(fake):
    client = fake(
        {
            "/v2/incidents/7": {"incident": {"id": "01INC"}},
            "/v3/actions": {
                "actions": [
                    {"id": "a1", "status": "outstanding", "assignee": USER, "description": "Fix"},
                    {"id": "a2", "status": "completed"},
                ]
            },
        },
        "incidents",
        "work",
    )
    result = await list_actions(incident="INC-7", status="outstanding", created_after="2026-10-01")
    call = client.calls[-1]
    assert call[2] == {"incident_id": "01INC", "created_at[gte]": "2026-10-01T00:00:00Z"}
    assert [item["id"] for item in result] == ["a1"]
    assert result[0]["assignee"]["slack_user_id"] == "U1"


async def test_follow_ups_include_team_and_external_issue(fake):
    client = fake(
        {
            "/v3/follow_ups": {
                "follow_ups": [
                    {
                        "id": "f1",
                        "title": "Add alert",
                        "status": "outstanding",
                        "priority": {"name": "High"},
                        "assignee_team": {"name": "Payments"},
                        "external_issue_reference": {
                            "provider": "jira",
                            "issue_name": "PLAT-1",
                            "issue_permalink": "https://jira/PLAT-1",
                        },
                        "labels": ["sre"],
                    }
                ]
            }
        },
        "work",
    )
    result = await list_follow_ups(team="team-1", updated_after="2026-10-01T00:00:00Z")
    assert client.calls[0][2] == {
        "assignee_team_id": "team-1",
        "updated_at[gte]": "2026-10-01T00:00:00Z",
    }
    assert result[0]["external_issue"] == {
        "provider": "jira",
        "name": "PLAT-1",
        "url": "https://jira/PLAT-1",
    }
    assert result[0]["assignee_team"] == "Payments"
    assert result[0]["priority"] == "High"


@pytest.mark.parametrize(
    ("fn", "kwargs"),
    [
        (list_actions, {"status": "open"}),
        (list_follow_ups, {"team": "bad id!"}),
        (list_follow_ups, {"created_after": "nope"}),
        (list_postmortems, {"incident": "INC-0"}),
        (get_postmortem, {"postmortem_id": "../x"}),
        (list_users, {"include_inactive": "yes"}),
        (list_users, {"slack_user_id": "U 1"}),
        (list_catalog_entries, {"catalog_type": ""}),
        (on_call, {"schedule": "Primary", "start": "2026-10-02", "end": "2026-10-01"}),
        (on_call, {"schedule": "Primary", "start": "2026-10-01", "end": "2026-11-15"}),
        (on_call, {"schedule": ""}),
    ],
)
async def test_invalid_inputs_make_no_request(fake, fn, kwargs):
    client = fake({}, "work", "directory", "on_call", "incidents")
    with pytest.raises(IncidentIoInputError):
        await fn(**kwargs)
    assert client.calls == []


async def test_postmortem_content_is_bounded(fake):
    fake(
        {
            "/v1/postmortem_documents/pm1": {
                "postmortem_document": {"id": "pm1", "title": "Checkout", "editors": [USER]}
            },
            "/v1/postmortem_documents/pm1/content": {"markdown": "# Notes\n" + "y" * 9000},
        },
        "work",
    )
    result = await get_postmortem("pm1")
    assert len(result["content"]) == 8000
    assert result["content_truncated"] is True
    assert result["editors"][0]["name"] == "Sam"


async def test_postmortems_list_newest_first(fake):
    client = fake({"/v1/postmortem_documents": {"postmortem_documents": [{"id": "pm1"}]}}, "work")
    await list_postmortems(limit=3)
    assert client.calls[0][2] == {"sort_by": "created_at_newest_first"}


async def test_teams_and_users_are_normalised(fake):
    fake(
        {
            "/v3/teams": {"teams": [{"id": "t1", "name": "Payments", "members": [USER]}]},
            "/v2/users": {
                "users": [{**USER, "is_active": True, "base_role": {"name": "Responder"}}]
            },
        },
        "directory",
    )
    assert await list_teams() == [
        {
            "id": "t1",
            "name": "Payments",
            "members": [{"id": "u1", "name": "Sam", "email": "s@x.io", "slack_user_id": "U1"}],
        }
    ]
    users = await list_users(email="s@x.io", include_inactive=True)
    assert users[0]["base_role"] == "Responder"
    assert users[0]["is_active"] is True


CATALOG_TYPES = [
    {
        "id": "ct1",
        "name": "Service",
        "type_name": 'Custom["Service"]',
        "schema": {"attributes": [{"id": "attr1", "name": "Owner", "type": "Team"}]},
        "estimated_count": 12,
    },
    {"id": "ct2", "name": "Team", "type_name": 'Custom["Team"]', "is_team_type": True},
]


async def test_catalog_types_and_entries_by_name(fake):
    client = fake(
        {
            "/v3/catalog_types": {"catalog_types": CATALOG_TYPES},
            "/v3/catalog_entries": {
                "catalog_entries": [
                    {
                        "id": "e1",
                        "name": "checkout",
                        "aliases": ["co"],
                        "attribute_values": {
                            "attr1": {"value": {"label": "Payments", "literal": "t1"}},
                            "attr9": {"array_value": [{"literal": "x"}, {"literal": "y"}]},
                            "empty": {},
                        },
                    }
                ]
            },
        },
        "directory",
    )
    types = await list_catalog_types()
    assert types[0]["attributes"] == [{"id": "attr1", "name": "Owner", "type": "Team"}]
    assert types[1]["is_team_type"] is True
    entries = await list_catalog_entries("service", identifier="checkout", limit=5)
    assert client.calls[-1][2] == {"catalog_type_id": "ct1", "identifier": "checkout"}
    assert entries[0]["attributes"] == {"Owner": ["Payments"], "attr9": ["x", "y"]}
    assert entries[0]["aliases"] == ["co"]


async def test_unknown_catalog_type_lists_names(fake):
    fake({"/v3/catalog_types": {"catalog_types": CATALOG_TYPES}}, "directory")
    with pytest.raises(IncidentIoInputError, match="valid names: Service, Team"):
        await list_catalog_entries("Feature")


SCHEDULES = [
    {
        "id": "s1",
        "name": "Primary",
        "timezone": "Europe/London",
        "config": {"rotations": [{"id": "r1", "name": "Weekly"}]},
        "current_shifts": [
            {"user": USER, "start_at": "2026-10-01T09:00:00Z", "end_at": "2026-10-08T09:00:00Z"}
        ],
        "next_shifts": [],
    },
    {"id": "s2", "name": "Secondary"},
]


async def test_on_call_default_window_and_final_entries(fake):
    client = fake(
        {
            "/v2/schedules": {"schedules": SCHEDULES},
            "/v2/schedule_entries": {
                "schedule_entries": {
                    "scheduled": [{"user": {"id": "ignored"}}],
                    "final": [{"user": USER, "start_at": "2026-10-01T09:00:00Z"}],
                }
            },
        },
        "on_call",
    )
    before = datetime.now(UTC).replace(microsecond=0)
    result = await on_call("primary")
    params = client.calls[-1][2]
    start = datetime.fromisoformat(params["entry_window_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(params["entry_window_end"].replace("Z", "+00:00"))
    assert params["schedule_id"] == "s1"
    assert start >= before
    assert (end - start).total_seconds() == 60
    assert [entry["user"]["id"] for entry in result["entries"]] == ["u1"]


async def test_on_call_start_only_uses_one_minute_window(fake):
    client = fake(
        {
            "/v2/schedules": {"schedules": SCHEDULES},
            "/v2/schedule_entries": {"schedule_entries": {"final": []}},
        },
        "on_call",
    )
    await on_call("s2", start="2026-10-01T09:00:00Z")
    params = client.calls[-1][2]
    assert params == {
        "schedule_id": "s2",
        "entry_window_start": "2026-10-01T09:00:00Z",
        "entry_window_end": "2026-10-01T09:01:00Z",
    }


async def test_unknown_schedule_lists_names(fake):
    fake({"/v2/schedules": {"schedules": SCHEDULES}}, "on_call")
    with pytest.raises(IncidentIoInputError, match="valid names: Primary, Secondary"):
        await on_call("Tertiary")


async def test_schedules_and_escalation_paths_are_normalised(fake):
    client = fake(
        {
            "/v2/schedules": {"schedules": SCHEDULES},
            "/v2/escalation_paths": {
                "escalation_paths": [
                    {
                        "id": "ep1",
                        "name": "Payments",
                        "team_ids": ["t1"],
                        "current_responders": [USER],
                        "path": [{"id": "l1"}, {"id": "l2"}],
                    }
                ]
            },
        },
        "on_call",
    )
    schedules = await list_schedules()
    assert schedules[0]["rotations"] == [{"id": "r1", "name": "Weekly"}]
    assert schedules[0]["current_shifts"][0]["user"]["name"] == "Sam"
    paths = await list_escalation_paths(limit=30)
    assert client.calls[-1][3:] == (30, 25)
    assert paths[0]["levels"] == 2
    assert paths[0]["current_responders"][0]["id"] == "u1"


def test_registration_exposes_exactly_the_public_surface():
    tools = plugin_tools()
    names = {key.split(".", 1)[1] for key in tools if key.startswith("incident_io.")}
    assert names == PUBLIC
    core = get_all_tools()
    for name in PUBLIC:
        fn = tools[f"incident_io.{name}"]
        assert fn._plugin == "incident_io" and fn._namespace == "incident_io"
        assert f"incident_io.{name}" not in core


def test_runner_exposes_incident_io_namespace(tmp_path, monkeypatch):
    from archie_plugins.incident_io import reference as reference_module

    class Client:
        async def items(self, path, key, params=None):
            assert path == "/v1/severities"
            return [{"id": "sev-1", "name": "Critical"}]

    @asynccontextmanager
    async def factory():
        yield Client()

    monkeypatch.setattr(sys.modules[reference_module.__module__], "incident_io_client", factory)
    (tmp_path / "main.py").write_text(
        "async def main():\n    return await incident_io.reference('severities')\n"
    )
    (tmp_path / "context.json").write_text(
        json.dumps(
            {
                "policy": {"incident_io": {"read": {"enabled": True}}},
                "settings": {},
                "enabled": ["incident_io"],
            }
        )
    )
    # run() installs contextvars without resetting them; isolate them from later tests.
    contextvars.copy_context().run(run, tmp_path)
    envelope = Envelope.read(tmp_path)
    assert envelope.ok is True, envelope.error
    assert envelope.return_value == [{"id": "sev-1", "name": "Critical", "description": None}]


@pytest.mark.parametrize(
    ("fn", "args", "responses", "module"),
    [
        (
            on_call,
            ("primary",),
            {
                "/v2/schedules": {
                    "schedules": [{"id": "s1", "name": "Primary"}, {"id": "s2", "name": "primary"}]
                }
            },
            "on_call",
        ),
        (
            list_catalog_entries,
            ("service",),
            {
                "/v3/catalog_types": {
                    "catalog_types": [
                        {"id": "c1", "name": "Service"},
                        {"id": "c2", "name": "SERVICE"},
                    ]
                }
            },
            "directory",
        ),
    ],
)
async def test_ambiguous_names_are_rejected_with_matching_ids(fake, fn, args, responses, module):
    client = fake(responses, module)
    with pytest.raises(IncidentIoInputError, match=r"matches more than one .*use an ID"):
        await fn(*args)
    assert len(client.calls) == 1  # only the lookup; the target endpoint is never called


async def test_alert_bare_date_upper_bound_covers_whole_day(fake):
    client = fake({"/v2/alerts": {"alerts": []}}, "alerts")
    await list_alerts(created_after="2026-10-01", created_before="2026-10-01")
    assert client.calls[0][2] == {
        "created_at[gte]": "2026-10-01T00:00:00Z",
        "created_at[lte]": "2026-10-01T23:59:59Z",
    }


async def test_on_call_reports_truncated_entries(fake):
    many = [{"user": {"id": f"u{i}"}} for i in range(60)]
    fake(
        {
            "/v2/schedules": {"schedules": [{"id": "s1", "name": "Primary"}]},
            "/v2/schedule_entries": {"schedule_entries": {"final": many}},
        },
        "on_call",
    )
    result = await on_call("s1", start="2026-10-01", end="2026-10-31")
    assert len(result["entries"]) == 50
    assert result["entries_truncated"] is True


async def test_tool_traffic_through_real_client_is_get_only(monkeypatch):
    """Drive a multi-request tool through the real client and a GET-only transport."""
    import httpx
    from archie_plugins.incident_io import client as client_module
    from archie_plugins.incident_io.incidents import get_incident

    methods = []

    def handler(request):
        methods.append(request.method)
        path = request.url.path
        if path == "/v2/incidents/5":
            return httpx.Response(200, json={"incident": {"id": "01INC", "reference": "INC-5"}})
        if path == "/v2/incident_participants":
            return httpx.Response(
                200, json={"incident_participants": {"active": [], "passive": []}}
            )
        return httpx.Response(200, json={"incident_alerts": [], "pagination_meta": {}})

    transport = httpx.MockTransport(handler)
    real = client_module.IncidentIoClient

    monkeypatch.setattr(client_module, "_credential", lambda: "k")
    monkeypatch.setattr(
        client_module, "IncidentIoClient", lambda key: real(key, transport=transport)
    )
    result = await get_incident("INC-5")
    assert result["reference"] == "INC-5"
    assert methods == ["GET", "GET", "GET"]
