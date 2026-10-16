"""incident.io timeline, update and alert tool tests with a fake client (no network)."""

import sys
from contextlib import asynccontextmanager

import pytest
from archie_agent.exec.tools.incident_io.alerts import get_alert, list_alerts
from archie_agent.exec.tools.incident_io.client import IncidentIoInputError
from archie_agent.exec.tools.incident_io.incidents import incident_timeline, incident_updates
from archie_shared.tool_policy import reset_policy_snapshot, set_policy_snapshot


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append(("get", path, dict(params or {})))
        return self.responses[path]

    async def items(self, path, key, params=None):
        self.calls.append(("items", path, dict(params or {})))
        return self.responses[path][key]

    async def paginate(self, path, key, limit, params=None, *, max_page_size=100, keep=None):
        self.calls.append(("paginate", path, dict(params or {}), limit, max_page_size))
        items = self.responses[path][key]
        if keep:
            items = [item for item in items if keep(item)]
        return items[:limit]


@pytest.fixture
def fake(monkeypatch):
    token = set_policy_snapshot({})

    def install(responses, *modules):
        client = FakeClient(responses)

        @asynccontextmanager
        async def factory():
            yield client

        for module in modules:
            target = sys.modules[f"archie_agent.exec.tools.incident_io.{module}"]
            monkeypatch.setattr(target, "incident_io_client", factory)
        return client

    yield install
    reset_policy_snapshot(token)


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
