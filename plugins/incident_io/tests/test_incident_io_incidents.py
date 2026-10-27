"""incident.io incident tool tests with a fake client (no network)."""

import pytest
from archie_plugins.incident_io.client import IncidentIoInputError
from archie_plugins.incident_io.incidents import get_incident, list_incidents
from archie_plugins.incident_io.reference import reference

SEVERITIES = [
    {"id": "sev-1", "name": "Critical", "rank": 3},
    {"id": "sev-2", "name": "Major", "rank": 2},
    {"id": "sev-3", "name": "SEV 3", "rank": 1},
]

INCIDENT = {
    "id": "01INC",
    "reference": "INC-42",
    "name": "Checkout errors",
    "summary": "Elevated 5xx " + "x" * 9000,
    "incident_status": {"id": "st-1", "name": "Investigating", "category": "live"},
    "severity": {"id": "sev-2", "name": "Major"},
    "incident_type": {"id": "t-1", "name": "Default"},
    "mode": "standard",
    "visibility": "public",
    "created_at": "2026-10-01T10:00:00.123Z",
    "updated_at": "2026-10-01T11:00:00Z",
    "creator": {"user": {"id": "u1", "name": "Alex", "email": "a@x.io", "role": "owner"}},
    "incident_role_assignments": [
        {"role": {"name": "Incident Lead"}, "assignee": {"id": "u2", "name": "Sam"}},
    ],
    "custom_field_entries": [
        {
            "custom_field": {"name": "Affected services"},
            "values": [{"value_catalog_entry": {"name": "checkout"}}, {"value_text": "api"}],
        }
    ],
    "incident_timestamp_values": [
        {"incident_timestamp": {"name": "Reported at"}, "value": {"value": "2026-10-01T10:00:00Z"}}
    ],
    "duration_metrics": [{"duration_metric": {"name": "Time to fix"}, "value_seconds": 600}],
    "postmortem_document_ids": ["pm-1"],
    "slack_channel_name": "inc-42-checkout",
    "permalink": "https://app.incident.io/x/incidents/42",
}


async def test_list_incidents_resolves_severity_names_and_encodes_filters(fake):
    client = fake(
        {
            "/v1/severities": {"severities": SEVERITIES},
            "/v2/incidents": {"incidents": [INCIDENT]},
        },
        "incidents",
    )
    result = await list_incidents(
        status_category=["live"],
        severity=["major", "SEV 3"],
        created_after="2026-10-01",
        limit=5,
    )
    paginate = [call for call in client.calls if call[0] == "paginate"][0]
    params = paginate[2]
    assert params["status_category[one_of]"] == ["live"]
    assert params["severity[one_of]"] == ["sev-2", "sev-3"]
    assert params["created_at[gte]"] == "2026-10-01"
    assert params["sort_by"] == "created_at_newest_first"
    assert "mode[one_of]" not in params
    assert paginate[3] == 5
    assert result[0]["reference"] == "INC-42"
    assert result[0]["severity"] == "Major"
    assert result[0]["status"] == {"name": "Investigating", "category": "live"}
    assert result[0]["created_at"] == "2026-10-01T10:00:00Z"
    assert "summary" not in result[0]


async def test_list_incidents_accepts_severity_id_and_mode(fake):
    client = fake(
        {"/v1/severities": {"severities": SEVERITIES}, "/v2/incidents": {"incidents": []}},
        "incidents",
    )
    await list_incidents(severity="sev-1", mode=["standard", "test"], sort="oldest")
    params = client.calls[-1][2]
    assert params["severity[one_of]"] == ["sev-1"]
    assert params["mode[one_of]"] == ["standard", "test"]
    assert params["sort_by"] == "created_at_oldest_first"


async def test_unknown_severity_name_lists_valid_names(fake):
    client = fake({"/v1/severities": {"severities": SEVERITIES}}, "incidents")
    with pytest.raises(IncidentIoInputError, match="valid names: Critical, Major, SEV 3"):
        await list_incidents(severity="Huge")
    assert not [call for call in client.calls if call[1] == "/v2/incidents"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"status_category": "bogus"},
        {"status_category": []},
        {"mode": "prod"},
        {"sort": "updated"},
        {"sort": ["newest"]},
        {"created_after": "yesterday"},
        {"created_after": "2026-10-02", "created_before": "2026-10-01"},
        {"created_after": "2026-10-01T10:00:00Z"},
        {"updated_before": "2026-13-01"},
        {"severity": ""},
        {"severity": "x" * 201},
    ],
)
async def test_invalid_input_makes_no_request(fake, kwargs):
    client = fake({}, "incidents")
    with pytest.raises(IncidentIoInputError):
        await list_incidents(**kwargs)
    assert client.calls == []


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ("inc-42", "/v2/incidents/42"),
        ("42", "/v2/incidents/42"),
        (42, "/v2/incidents/42"),
        ("01INC", "/v2/incidents/01INC"),
    ],
)
async def test_get_incident_normalises_identifier(fake, value, path):
    client = fake(
        {
            path: {"incident": INCIDENT},
            "/v2/incident_participants": {"incident_participants": {"active": [], "passive": []}},
            "/v2/incident_alerts": {"incident_alerts": []},
        },
        "incidents",
    )
    await get_incident(value)
    assert client.calls[0][1] == path


@pytest.mark.parametrize("value", ["INC-0", "INC-x", "../etc", "a/b", "", None, True])
async def test_get_incident_rejects_bad_identifier(fake, value):
    client = fake({}, "incidents")
    with pytest.raises(IncidentIoInputError):
        await get_incident(value)
    assert client.calls == []


async def test_get_incident_detail_includes_participants_alerts_and_bounds(fake):
    fake(
        {
            "/v2/incidents/42": {"incident": INCIDENT},
            "/v2/incident_participants": {
                "incident_participants": {
                    "active": [{"user": {"id": "u2", "name": "Sam"}, "participant_type": "x"}],
                    "passive": [{"user": {"id": "u3", "name": "Kim"}}],
                }
            },
            "/v2/incident_alerts": {
                "incident_alerts": [{"alert": {"id": "al-1"}}, {"alert": {"id": "al-1"}}]
            },
        },
        "incidents",
    )
    detail = await get_incident("INC-42")
    assert detail["participants"]["active"][0]["name"] == "Sam"
    assert detail["participants"]["passive"][0]["id"] == "u3"
    assert detail["alert_ids"] == ["al-1"]
    assert detail["summary"] == "Elevated 5xx " + "x" * 9000
    assert detail["creator"]["name"] == "Alex"
    assert detail["role_assignments"] == [
        {
            "role": "Incident Lead",
            "assignee": {"id": "u2", "name": "Sam", "email": None, "slack_user_id": None},
        }
    ]
    assert detail["custom_fields"] == [{"name": "Affected services", "values": ["checkout", "api"]}]
    assert detail["timestamps"] == [{"name": "Reported at", "value": "2026-10-01T10:00:00Z"}]
    assert detail["durations"] == [{"name": "Time to fix", "seconds": 600}]
    assert "incident_status" not in detail


async def test_reference_lists_kind_and_rejects_unknown(fake):
    fake({"/v1/severities": {"severities": SEVERITIES}}, "reference")
    result = await reference("severities")
    assert result[0] == {"id": "sev-1", "name": "Critical", "description": None, "rank": 3}
    with pytest.raises(IncidentIoInputError, match="kind must be one of"):
        await reference("pay_configs")


async def test_ambiguous_severity_name_is_rejected(fake):
    client = fake(
        {
            "/v1/severities": {
                "severities": [{"id": "a", "name": "Major"}, {"id": "b", "name": "major"}]
            }
        },
        "incidents",
    )
    with pytest.raises(IncidentIoInputError, match=r"matches more than one severity.*Major \(a\)"):
        await list_incidents(severity="MAJOR")
    assert [call[1] for call in client.calls] == ["/v1/severities"]
