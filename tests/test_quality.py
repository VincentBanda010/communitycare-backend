"""Synthetic fixtures validate software behaviour, not clinical effectiveness."""
from types import SimpleNamespace
import httpx
import pytest
from fastapi import HTTPException
from app.quality import ReportingQuality, analyse

ORG = "Abcdef12345"
SETTINGS = SimpleNamespace(program="Pbcdef12345", stage="Sbcdef12345", triage_element="Dbcdef12345",
                           dhis_url="https://dhis.example", dhis_token="test-only")


def events():
    return [{"event": f"Ebcdef1234{i}", "orgUnit": ORG, "program": SETTINGS.program,
             "programStage": SETTINGS.stage, "occurredAt": f"2024-01-{i+1:02}T08:00:00Z",
             "createdAt": f"2024-01-{i+1:02}T08:{i:02}:00Z",
             "dataValues": [{"dataElement": SETTINGS.triage_element, "value": "REFERRAL"}]} for i in range(6)]


def test_snapshot_trains_real_model_on_passed_rows_and_is_deterministic():
    first = analyse(events(), ORG, SETTINGS)
    second = analyse(list(reversed(events())), ORG, SETTINGS)
    assert first["modelStatus"] == "EXPERIMENTAL_SMALL_SAMPLE"
    assert first["trainingSamples"] == 6
    assert first["categoryCounts"]["REFERRAL"] == 6
    assert first["candidates"] == second["candidates"]
    assert len({c["anomalyScore"] for c in first["candidates"]}) > 1
    changed = events(); changed[0]["createdAt"] = "2024-02-01T08:00:00Z"
    result = analyse(changed, ORG, SETTINGS)
    assert result["datasetFingerprint"] != first["datasetFingerprint"]
    assert result["delayedOver24Hours"] == 1
    assert result["candidates"] != first["candidates"]


def test_too_few_rows_produce_no_fabricated_scores():
    result = analyse(events()[:2], ORG, SETTINGS)
    assert result["modelStatus"] == "INSUFFICIENT_DATA"
    assert result["candidates"] == []
    assert result["trainingSamples"] == 2


def test_bad_fields_count_as_invalid_and_cross_scope_is_refused():
    rows = events(); rows[0]["dataValues"] = []
    rows[1]["occurredAt"] = "not a date"
    result = analyse(rows, ORG, SETTINGS)
    assert result["invalidCount"] == 2 and result["validCount"] == 4
    assert result["modelStatus"] == "INSUFFICIENT_DATA"
    rows = events(); rows[0]["orgUnit"] = "Zbcdef12345"
    with pytest.raises(HTTPException) as error:
        analyse(rows, ORG, SETTINGS)
    assert error.value.status_code == 502


def test_reader_uses_exact_scope_and_minimum_fields_then_caches():
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.params["orgUnit"] == ORG
        assert request.url.params["orgUnitMode"] == "SELECTED"
        assert "storedBy" not in request.url.params["fields"]
        return httpx.Response(200, json={"events": events()})
    reader = ReportingQuality(SETTINGS, client=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        assert reader.snapshot(ORG)["validCount"] == 6
        assert reader.snapshot(ORG)["validCount"] == 6
        assert len(requests) == 1
        reader.cache[ORG] = (0, reader.cache[ORG][1])
        assert reader.snapshot(ORG)["validCount"] == 6
        assert len(requests) == 2
    finally:
        reader.close()


def test_source_failure_never_falls_back_to_stale_success():
    fail = False
    def handle(request):
        return httpx.Response(503) if fail else httpx.Response(200, json={"events": events()})
    reader = ReportingQuality(SETTINGS, client=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        reader.snapshot(ORG)
        reader.cache[ORG] = (0, reader.cache[ORG][1]); fail = True
        with pytest.raises(HTTPException) as error:
            reader.snapshot(ORG)
        assert error.value.status_code == 502
    finally:
        reader.close()
