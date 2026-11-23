"""Opt-in real PostgreSQL tests: use a dedicated test database."""
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.models import Encounter
from app.queue import SyncQueue

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not configured")


@pytest.fixture
def queue():
    settings = SimpleNamespace(
        database_url=os.environ["TEST_DATABASE_URL"],
        database_user=os.environ.get("TEST_DATABASE_USER", "communitycare"),
        database_password=os.environ["TEST_DATABASE_PASSWORD"], key=bytes(range(32)),
    )
    storage = SyncQueue(settings)
    storage.open()
    org = "T" + uuid4().hex[:10]
    try:
        yield storage, org
    finally:
        with storage.pool.connection() as connection:
            connection.execute("DELETE FROM sync_event WHERE org_unit=%s", (org,))
        storage.close()


def test_persistent_deduplication_encryption_and_receipts(queue):
    storage, org = queue
    identity = uuid4()
    encounter = Encounter(occurredAt="2023-10-26T09:30:00.123456789+02:00", triage="REFERRAL")
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(executor.map(lambda _: storage.enqueue(org, identity, encounter), range(8)))
    assert all(item["state"] == "QUEUED" for item in receipts)
    with pytest.raises(HTTPException) as conflict:
        storage.enqueue(org, identity, Encounter(occurredAt=encounter.occurredAt, triage="ROUTINE"))
    assert conflict.value.status_code == 409
    assert storage.find("Z" + org[1:], identity) is None
    with storage.pool.connection() as connection:
        rows = connection.execute("SELECT * FROM sync_event WHERE org_unit=%s", (org,)).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert b"REFERRAL" not in row["payload"]
    assert storage.read(row) == ["2023-10-26T07:30:00.123456789Z", "REFERRAL"]
    # Check lease guards without consuming unrelated jobs in this test database.
    lease = uuid4()
    with storage.pool.connection() as connection:
        connection.execute("UPDATE sync_event SET lease_id=%s WHERE org_unit=%s", (lease, org))
    row["lease_id"] = uuid4()
    storage.finish(row, "FORWARDED")
    assert storage.find(org, identity)["state"] == "QUEUED"
    row["lease_id"] = lease
    storage.retry(row)
    with storage.pool.connection() as connection:
        updated = connection.execute("SELECT * FROM sync_event WHERE org_unit=%s", (org,)).fetchone()
        assert updated["attempts"] == 1
        assert updated["lease_id"] is None
        connection.execute("UPDATE sync_event SET lease_id=%s WHERE org_unit=%s", (lease, org))
    storage.finish(row, "FORWARDED")
    with storage.pool.connection() as connection:
        assert connection.execute("SELECT payload FROM sync_event WHERE org_unit=%s", (org,)).fetchone()["payload"] is None
    assert storage.enqueue(org, identity, encounter)["state"] == "FORWARDED"


def test_concurrent_claims_are_distinct(queue):
    storage, org = queue
    # Requires a dedicated database with no other queued work.
    encounter = Encounter(occurredAt="2023-10-26T09:30:00Z", triage="ROUTINE")
    for _ in range(4):
        storage.enqueue(org, uuid4(), encounter)
    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = list(executor.map(lambda _: storage.claim(), range(4)))
    assert all(job is not None for job in jobs)
    assert len({job["encounter_id"] for job in jobs}) == 4
    assert storage.claim() is None
