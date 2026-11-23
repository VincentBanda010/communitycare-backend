"""Read-only, organisation-scoped reporting-quality ML. Never clinical inference."""
from collections import Counter, OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
import time

import httpx
from fastapi import HTTPException
import numpy as np
from sklearn.ensemble import IsolationForest

VERSION = "reporting-iforest-v1"
LIMIT = 200
MIN_SAMPLES = 5
CACHE_SECONDS = 10
CATEGORIES = ("ROUTINE", "REFERRAL", "URGENT_REFERRAL")


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # The hosted DHIS2 service runs in UTC and may omit the offset on createdAt.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def analyse(events, org, settings, *, truncated=False):
    now = datetime.now(timezone.utc)
    rows, invalid, seen = [], 0, set()
    for event in events:
        # Defence in depth: never score events outside the caller's exact scope.
        if (event.get("orgUnit") != org or event.get("program") != settings.program
                or event.get("programStage") != settings.stage):
            raise HTTPException(502, "Reporting source returned an unexpected scope")
        try:
            identity = event["event"]
            if not isinstance(identity, str) or not identity or identity in seen:
                raise ValueError("Invalid event identity")
            occurred, created = utc(event["occurredAt"]), utc(event["createdAt"])
            values = [v["value"] for v in event.get("dataValues", [])
                      if v.get("dataElement") == settings.triage_element]
            if len(values) != 1 or values[0] not in CATEGORIES or occurred > now or created > now:
                raise ValueError("Invalid report fields")
            delay = (created - occurred).total_seconds() / 3600
            if delay < -0.01:
                raise ValueError("Arrival predates assessment")
            rows.append({"eventId": identity, "occurredAt": occurred.isoformat(),
                         "createdAt": created.isoformat(), "category": values[0],
                         "delayHours": max(0, delay)})
            seen.add(identity)
        except (ValueError, TypeError, KeyError, AttributeError):
            invalid += 1
    rows.sort(key=lambda r: r["eventId"])
    counts = Counter(r["category"] for r in rows)
    delayed = sum(r["delayHours"] > 24 for r in rows)
    fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16]
    result = {"orgUnit": org, "generatedAt": now.isoformat(), "source": "OWNED_DHIS2",
              "dataContext": "PILOT_INCLUDES_ENGINEERING_TEST_RECORDS",
              "reportCount": len(events), "validCount": len(rows), "invalidCount": invalid,
              "categoryCounts": {c: counts[c] for c in CATEGORIES},
              "delayedOver24Hours": delayed, "snapshotLimit": LIMIT, "truncated": truncated,
              "cacheSeconds": CACHE_SECONDS, "modelVersion": VERSION,
              "datasetFingerprint": fingerprint, "modelStatus": "INSUFFICIENT_DATA",
              "trainingSamples": len(rows), "minimumSamples": MIN_SAMPLES,
              "fittedOn": "CURRENT_SNAPSHOT", "candidates": [],
              "notice": "Experimental reporting review only. Scores are not probabilities, diagnoses or evidence of staff performance. The pilot includes engineering test records; clinical effectiveness is unvalidated."}
    if len(rows) < MIN_SAMPLES:
        return result
    features = []
    for row in rows:
        stamp = utc(row["occurredAt"])
        hour = stamp.hour + stamp.minute / 60
        features.append([math.log1p(row["delayHours"]), math.sin(hour * math.tau / 24),
                         math.cos(hour * math.tau / 24), math.sin(stamp.weekday() * math.tau / 7),
                         math.cos(stamp.weekday() * math.tau / 7),
                         *[float(row["category"] == c) for c in CATEGORIES]])
    data = np.asarray(features, dtype=float)
    if not np.isfinite(data).all():
        raise HTTPException(502, "Reporting source contains invalid measurements")
    # The actual model fits the authorised live snapshot; no generated rows enter training.
    model = IsolationForest(n_estimators=100, max_samples="auto", contamination="auto",
                            random_state=42, n_jobs=1)
    model.fit(data)
    scores = -model.score_samples(data)
    candidates = [{"eventId": row["eventId"], "occurredAt": row["occurredAt"],
                   "category": row["category"], "delayHours": round(row["delayHours"], 3),
                   "anomalyScore": round(float(score), 6),
                   "reviewSuggested": bool(score >= 0.65)} for row, score in zip(rows, scores)]
    candidates.sort(key=lambda r: (-r["anomalyScore"], r["eventId"]))
    result.update(modelStatus="EXPERIMENTAL_SMALL_SAMPLE" if len(rows) < 30 else "EXPERIMENTAL_UNVALIDATED",
                  candidates=candidates[:5])
    return result


class ReportingQuality:
    def __init__(self, settings, *, client=None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=False)
        self.lock = threading.Lock()
        self.cache = OrderedDict()

    def close(self):
        self.client.close()

    def snapshot(self, org):
        if not self.lock.acquire(blocking=False):
            raise HTTPException(503, "Reporting analysis is busy; retry shortly")
        try:
            entry = self.cache.get(org)
            if entry and time.monotonic() - entry[0] < CACHE_SECONDS:
                return entry[1]
            try:
                with self.client.stream("GET", self.settings.dhis_url + "/api/tracker/events",
                                        params={"program": self.settings.program, "orgUnit": org,
                                                "orgUnitMode": "SELECTED", "pageSize": LIMIT,
                                                "page": 1, "order": "createdAt:desc",
                                                "fields": "event,program,programStage,orgUnit,occurredAt,createdAt,dataValues[dataElement,value]"},
                                        headers={"Authorization": "ApiToken " + self.settings.dhis_token}) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 2_000_000:
                            raise ValueError("Snapshot exceeds size limit")
                        chunks.append(chunk)
                    body = json.loads(b"".join(chunks))
                events = body.get("events", body.get("instances"))
                if not isinstance(events, list) or len(events) > LIMIT or not all(isinstance(e, dict) for e in events):
                    raise ValueError("Unexpected reporting response")
                result = analyse(events, org, self.settings, truncated=len(events) == LIMIT)
            except HTTPException:
                raise
            except Exception:
                # No fallback model scores or stale successful snapshot on source failure.
                raise HTTPException(502, "Live reporting analysis is unavailable") from None
            self.cache[org] = (time.monotonic(), result)
            self.cache.move_to_end(org)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return result
        finally:
            self.lock.release()
