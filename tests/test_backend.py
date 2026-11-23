import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import httpx
import jwt
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.auth import TokenVerifier
from app.config import Settings
from app.delivery import DeliveryWorker, outcome
from app.main import create_app
from app.models import Encounter, instant
from app.queue import Crypto

BODY = {"occurredAt": "2023-10-26T09:30:00+02:00", "triage": "REFERRAL"}
ORG = "Abcdef12345"


class MemoryQueue:
    def __init__(self):
        self.rows = {}
        self.states = []
        self.retries = 0

    def open(self): pass
    def close(self): pass
    def healthy(self): return True

    def enqueue(self, org, encounter_id, encounter):
        key = org, encounter_id
        payload = instant(encounter.occurredAt), encounter.triage
        previous = self.rows.get(key)
        if previous and previous[0] != payload:
            raise HTTPException(409)
        self.rows.setdefault(key, (payload, "QUEUED"))
        return self.find(org, encounter_id)

    def find(self, org, encounter_id):
        row = self.rows.get((org, encounter_id))
        return {"encounterId": str(encounter_id), "state": row[1]} if row else None

    def read(self, job): return "2023-10-26T07:30:00Z", "REFERRAL"
    def finish(self, job, state): self.states.append(state)
    def retry(self, job): self.retries += 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = TokenVerifier.__new__(TokenVerifier)
    verifier.issuer, verifier.audience = "https://issuer.example", "communitycare"
    verifier.keys = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private_key.public_key()))
    settings = SimpleNamespace(update_directory=tmp_path)
    queue = MemoryQueue()
    app = create_app(settings, queue, verifier, start_worker=False)

    def token(**changes):
        claims = {"iss": verifier.issuer, "aud": verifier.audience, "sub": "worker",
                  "exp": datetime.now(timezone.utc) + timedelta(minutes=5), "dhis_org_unit": ORG}
        claims.update(changes)
        return {"Authorization": "Bearer " + jwt.encode(claims, private_key, algorithm="RS256")}

    with TestClient(app) as client:
        yield client, token, queue, tmp_path


def test_authentication_and_claims(api):
    client, token, _, _ = api
    path = f"/api/v1/encounters/{uuid4()}"
    assert client.get("/actuator/health").json() == {"status": "UP"}
    assert client.get(path).status_code == 401
    for claims in ({"aud": "other"}, {"iss": "https://other.example"}, {"exp": 1}):
        assert client.get(path, headers=token(**claims)).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer broken"}).status_code == 401
    assert client.get(path, headers=token(dhis_org_unit="bad")).status_code == 403


def test_mobile_profile_requires_authorized_organisation(api):
    client, token, _, _ = api
    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me", headers=token(dhis_org_unit="bad")).status_code == 403
    response = client.get("/api/v1/me", headers=token(name="Community worker", preferred_username="worker01"))
    assert response.status_code == 200
    assert response.json() == {"subject": "worker", "username": "worker01", "name": "Community worker", "orgUnit": ORG}
    assert response.headers["cache-control"] == "no-store"


def test_quality_requires_auth_and_original_facility(api, monkeypatch):
    client, token, _, _ = api
    calls = []
    def snapshot(self, org):
        calls.append(org)
        return {"orgUnit": org, "reportCount": 6}
    monkeypatch.setattr("app.quality.ReportingQuality.snapshot", snapshot)
    path = "/api/v1/reporting/quality"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=token(dhis_org_unit="bad")).status_code == 403
    wrong = {**token(), "X-Organisation-Unit": "Zbcdef12345"}
    assert client.get(path, headers=wrong).status_code == 403
    assert calls == []
    response = client.get(path, headers={**token(), "X-Organisation-Unit": ORG})
    assert response.status_code == 200 and response.json()["orgUnit"] == ORG
    assert calls == [ORG]
    assert response.headers["cache-control"] == "no-store"


def test_mobile_report_cannot_move_facilities_after_token_refresh(api):
    client, token, queue, _ = api
    identity = uuid4()
    path = f"/api/v1/encounters/{identity}"
    reassigned = {**token(dhis_org_unit="Zbcdef12345"), "X-Organisation-Unit": ORG}
    assert client.put(path, headers=reassigned, json=BODY).status_code == 403
    assert client.get(path, headers=reassigned).status_code == 403
    assert queue.rows == {}
    original = {**token(), "X-Organisation-Unit": ORG}
    assert client.put(path, headers=original, json=BODY).status_code == 202


def test_sync_retries_conflicts_and_org_isolation(api):
    client, token, queue, _ = api
    identity = uuid4()
    path = f"/api/v1/encounters/{identity}"
    first = client.put(path, headers=token(), json=BODY)
    assert first.status_code == 202
    assert first.json() == {"encounterId": str(identity), "state": "QUEUED"}
    assert client.put(path, headers=token(), json=BODY).status_code == 202
    equivalent = {**BODY, "occurredAt": "2023-10-26T07:30:00Z"}
    assert client.put(path, headers=token(), json=equivalent).status_code == 202
    assert client.put(path, headers=token(), json={**BODY, "triage": "ROUTINE"}).status_code == 409
    assert client.get(path, headers=token(dhis_org_unit="Zbcdef12345")).status_code == 404
    payload, _ = queue.rows[(ORG, identity)]
    queue.rows[(ORG, identity)] = payload, "FORWARDED"
    assert client.put(path, headers=token(), json=BODY).status_code == 200
    assert client.get(path, headers=token()).json()["state"] == "FORWARDED"


@pytest.mark.parametrize("body", [
    {**BODY, "patientName": "excluded"}, {**BODY, "triage": 1},
    {**BODY, "triage": "UNKNOWN"}, {**BODY, "occurredAt": "2023-10-26T09:30:00"},
    {**BODY, "occurredAt": "2999-01-01T00:00:00Z"}, {**BODY, "occurredAt": 123}, {},
])
def test_invalid_input(api, body):
    client, token, _, _ = api
    assert client.put(f"/api/v1/encounters/{uuid4()}", json=body, headers=token()).status_code == 422


def test_health_failure_does_not_expose_database_errors(api):
    client, _, queue, _ = api
    def failure(): raise RuntimeError("database secret")
    queue.healthy = failure
    response = client.get("/actuator/health")
    assert response.status_code == 503
    assert response.json() == {"status": "DOWN"}


def test_update_downloads(api):
    client, token, _, directory = api
    (directory / "manifest.jws").write_text("signed.content.signature")
    filename = "a" * 64 + ".zip"
    (directory / filename).write_bytes(b"zip-content")
    for name, content_type in (("manifest.jws", "application/jose"), (filename, "application/zip")):
        path = "/api/v1/updates/" + name
        assert client.get(path).status_code == 401
        response = client.get(path, headers=token())
        assert response.status_code == 200
        assert response.headers["content-type"] == content_type
        assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/updates/.env", headers=token()).status_code == 404
    assert client.get("/api/v1/updates/" + "b" * 64 + ".zip", headers=token()).status_code == 404


@pytest.mark.parametrize("timestamp, expected", [
    ("2023-10-26T09:30:00+02:00", "2023-10-26T07:30:00Z"),
    ("2023-10-26T00:30:00.1+02:00", "2023-10-25T22:30:00.100Z"),
    ("2023-10-26T09:30:00.000100Z", "2023-10-26T09:30:00.000100Z"),
    ("2023-10-26T09:30:00.123456789Z", "2023-10-26T09:30:00.123456789Z"),
])
def test_java_timestamp_compatibility(timestamp, expected):
    assert instant(Encounter(occurredAt=timestamp, triage="REFERRAL").occurredAt) == expected


def test_encryption_integrity_and_context_binding():
    # NIST AES-256-GCM known-answer vector: empty plaintext/AAD, zero key/nonce.
    assert Crypto(bytes(32)).decrypt("", bytes(12) + bytes.fromhex("530f8afbc74536b9a963b4f1c4cb738b")) == b""
    crypto = Crypto(bytes(range(32)))
    encrypted = crypto.encrypt("org/id", b"timestamp|REFERRAL")
    assert crypto.decrypt("org/id", encrypted) == b"timestamp|REFERRAL"
    assert b"REFERRAL" not in encrypted
    with pytest.raises(InvalidTag): crypto.decrypt("other/id", encrypted)
    with pytest.raises(InvalidTag): crypto.decrypt("org/id", encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
    assert crypto.event_uid("org/id") == crypto.event_uid("org/id")
    assert crypto.event_uid("org/id") != crypto.event_uid("other/id")


def test_configuration_validation(monkeypatch, tmp_path):
    environment = {
        "DATABASE_URL": "jdbc:postgresql://localhost:5432/communitycare",
        "DATABASE_PASSWORD": "test", "OIDC_ISSUER": "https://issuer.example",
        "OIDC_AUDIENCE": "communitycare", "SYNC_ENCRYPTION_KEY": base64.b64encode(bytes(32)).decode(),
        "DHIS2_URL": "https://dhis.example/instance", "DHIS2_TOKEN": "test",
        "DHIS2_PROGRAM": ORG, "DHIS2_STAGE": ORG, "DHIS2_TRIAGE_ELEMENT": ORG,
        "UPDATE_DIRECTORY": str(tmp_path),
    }
    for name, value in environment.items(): monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.database_url.startswith("postgresql:")
    assert settings.key == bytes(32)
    monkeypatch.setenv("DHIS2_URL", "http://dhis.example")
    with pytest.raises(ValueError): Settings.from_env()
    monkeypatch.setenv("DHIS2_URL", environment["DHIS2_URL"])
    monkeypatch.setenv("SYNC_ENCRYPTION_KEY", base64.b64encode(bytes(16)).decode())
    with pytest.raises(ValueError): Settings.from_env()


@pytest.mark.parametrize("status, report, expected", [
    (200, {"status": "OK", "bundleReport": {"typeReportMap": {"EVENT": {"stats": {"created": 1}}}}}, "FORWARDED"),
    (400, {"validationReport": {"errorReports": [{"errorCode": "E1000"}]}}, "REJECTED"),
    (409, {"status": "ERROR", "validationReport": {"errorReports": [{"errorCode": "E1091"}]}}, "REJECTED"),
    (500, {"validationReport": {"errorReports": [1]}}, None),
    (401, {}, None), (200, {"status": "OK"}, None), (200, [], None),
])
def test_delivery_outcome(status, report, expected):
    assert outcome(status, report) == expected


@pytest.mark.parametrize("remote_status, body, expected", [
    (200, {"status": "OK", "bundleReport": {"typeReportMap": {"EVENT": {"stats": {"updated": 1}}}}}, "FORWARDED"),
    (400, {"validationReport": {"errorReports": [1]}}, "REJECTED"),
    (503, {}, None), (200, "not-json", None),
])
def test_worker_sends_correct_tracker_contract(remote_status, body, expected):
    queue = MemoryQueue()
    settings = SimpleNamespace(program="program1234", stage="stage123456", triage_element="triage12345",
                               dhis_url="https://example.com/dhis", dhis_token="secret")
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(remote_status, json=body) if not isinstance(body, str) else httpx.Response(remote_status, text=body)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    worker = DeliveryWorker(queue, settings, client)
    job = {"event_uid": "Event123456", "org_unit": ORG}
    worker.deliver(job)
    worker.close()
    event = json.loads(requests[0].content)["events"][0]
    assert event["event"] == job["event_uid"]
    assert event["dataValues"] == [{"dataElement": settings.triage_element, "value": "REFERRAL"}]
    assert requests[0].url.path == "/dhis/api/tracker"
    assert queue.states == ([expected] if expected else [])
    assert queue.retries == (0 if expected else 1)
