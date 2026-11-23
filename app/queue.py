import hmac
import secrets
from pathlib import Path
from uuid import uuid4
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from .models import instant


class Crypto:
    def __init__(self, key):
        self.key = key
        self.aes = AESGCM(key)

    def mac(self, value):
        return hmac.digest(self.key, value.encode(), "sha256")

    def encrypt(self, context, payload):
        nonce = secrets.token_bytes(12)
        return nonce + self.aes.encrypt(nonce, payload, context.encode())

    def decrypt(self, context, payload):
        return self.aes.decrypt(payload[:12], payload[12:], context.encode())

    def event_uid(self, context):
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        alphabet = letters + "0123456789"
        number = int.from_bytes(self.mac("event/" + context), "big")
        suffix = ""
        for _ in range(10):
            number, remainder = divmod(number, 62)
            suffix += alphabet[remainder]
        return letters[number % 52] + suffix[::-1]


class SyncQueue:
    def __init__(self, settings):
        self.crypto = Crypto(settings.key)
        self.pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=10, open=False,
            kwargs={"user": settings.database_user, "password": settings.database_password,
                    "row_factory": dict_row, "connect_timeout": 5},
        )

    def open(self):
        self.pool.open(wait=True, timeout=15)
        # Serialize initial schema creation across API replicas. Existing Java rows
        # and Flyway history are left intact; the queue schema is compatible.
        with self.pool.connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(763204910)")
            connection.execute((Path(__file__).parent / "schema.sql").read_text())

    def close(self):
        self.pool.close()

    def healthy(self):
        with self.pool.connection() as connection:
            connection.execute("SELECT 1")
        return True

    def enqueue(self, org, encounter_id, encounter):
        context = f"{org}/{encounter_id}"
        payload = f"{instant(encounter.occurredAt)}|{encounter.triage.value}".encode()
        digest = self.crypto.mac(context + "|" + payload.decode())
        with self.pool.connection() as connection:
            connection.execute(
                """INSERT INTO sync_event(org_unit, encounter_id, event_uid, payload, payload_digest)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT(org_unit, encounter_id) DO NOTHING""",
                (org, encounter_id, self.crypto.event_uid(context), self.crypto.encrypt(context, payload), digest),
            )
            row = connection.execute(
                "SELECT payload_digest, state FROM sync_event WHERE org_unit=%s AND encounter_id=%s",
                (org, encounter_id),
            ).fetchone()
            if not hmac.compare_digest(digest, row["payload_digest"]):
                raise HTTPException(409, "Encounter ID already used")
            return {"encounterId": str(encounter_id), "state": row["state"]}

    def find(self, org, encounter_id):
        with self.pool.connection() as connection:
            row = connection.execute(
                "SELECT state FROM sync_event WHERE org_unit=%s AND encounter_id=%s", (org, encounter_id),
            ).fetchone()
        return {"encounterId": str(encounter_id), "state": row["state"]} if row else None

    def claim(self):
        with self.pool.connection() as connection:
            return connection.execute(
                """WITH candidate AS (
                     SELECT org_unit, encounter_id FROM sync_event
                     WHERE state='QUEUED' AND next_attempt<=now()
                     ORDER BY next_attempt LIMIT 1 FOR UPDATE SKIP LOCKED
                   ) UPDATE sync_event s SET next_attempt=now()+interval '60 seconds', lease_id=%s
                   FROM candidate c WHERE s.org_unit=c.org_unit AND s.encounter_id=c.encounter_id
                   RETURNING s.*""", (uuid4(),),
            ).fetchone()

    def read(self, job):
        context = f"{job['org_unit']}/{job['encounter_id']}"
        return self.crypto.decrypt(context, job["payload"]).decode().split("|", 1)

    def finish(self, job, state):
        if state not in ("FORWARDED", "REJECTED"):
            raise ValueError("Invalid terminal state")
        with self.pool.connection() as connection:
            connection.execute(
                """UPDATE sync_event SET state=%s,payload=NULL,lease_id=NULL
                   WHERE org_unit=%s AND encounter_id=%s AND lease_id=%s""",
                (state, job["org_unit"], job["encounter_id"], job["lease_id"]),
            )

    def retry(self, job):
        delay = min(3600, 15 * 2 ** min(job["attempts"], 8))
        with self.pool.connection() as connection:
            connection.execute(
                """UPDATE sync_event SET attempts=LEAST(attempts+1,9),lease_id=NULL,
                   next_attempt=now()+(%s * interval '1 second')
                   WHERE org_unit=%s AND encounter_id=%s AND lease_id=%s""",
                (delay, job["org_unit"], job["encounter_id"], job["lease_id"]),
            )
