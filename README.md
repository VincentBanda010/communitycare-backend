# CommunityCare AI Python backend

**Live hosted deployment:** DHIS2, PostgreSQL, Keycloak and the Python API now run
on Contabo. See [deployment and operator instructions](deploy/CONTABO.md) and
`CONTABO_CONNECTION_CHECK.json`. The public-demo configuration further below is
retained as historical setup guidance; it is not used by the hosted deployment.

FastAPI backend for Android encounter synchronization, queued DHIS2 delivery and
serving of pre-published update files. The current Android app records health-worker-confirmed
triage outcomes offline; it does not perform AI assessment or generate referrals. The
reporting contract accepts only a timestamp and the worker-confirmed triage category.

Current 1.5 preview adds [staff roles, approval and shared task workflows](docs/STAFF_ROLES.md), with the original mobile dashboard restored.

## Local setup (PowerShell)

Requires Python 3.11+ and a running PostgreSQL database.

```powershell
cd C:\Projects\communitycare-backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Fill in `.env`, then start the API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Generate a persistent encryption key once:

```powershell
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Never commit `.env`. Retain the same key when migrating Java queue data and across
replicas/restarts. Changing it requires a separate data migration. Startup validates
configuration, connects to PostgreSQL, creates missing queue objects under a
database advisory lock, discovers the issuer's public signing keys, and starts the
delivery worker. Stop with Ctrl+C. Java and Maven are no longer required.

| Variable | Purpose |
| --- | --- |
| DATABASE_URL | PostgreSQL URL; default `postgresql://localhost:5432/communitycare`. Legacy `jdbc:postgresql://...` is accepted. |
| DATABASE_USER | PostgreSQL user; default `communitycare`. |
| DATABASE_PASSWORD | Required database password. |
| OIDC_ISSUER | Exact HTTPS issuer from the Android access token. Must support OIDC discovery. |
| OIDC_AUDIENCE | Required API audience. |
| SYNC_ENCRYPTION_KEY | Base64-encoded 32-byte AES key. |
| DHIS2_URL | HTTPS DHIS2 base URL, including any instance path. |
| DHIS2_TOKEN | Service user's DHIS2 personal access token. |
| DHIS2_PROGRAM | 11-character event-program UID (without registration). |
| DHIS2_STAGE | 11-character program-stage UID. |
| DHIS2_TRIAGE_ELEMENT | 11-character triage data-element UID. |
| UPDATE_DIRECTORY | Published files directory; default `./updates`. |

## Android API (unchanged routes)

Protected `/api/v1` routes require an RS256 OAuth bearer token. The native account registration, login, refresh and password-reset routes have the documented public exception. Signature, expiration,
issuer and audience are checked; `sub` is required. Encounter routes additionally
require a server-assigned `dhis_org_unit` claim containing an 11-character UID.
Users in one organisation unit share its receipts. Public health does not require
a token. Unknown body fields are rejected.

The Android client sends `X-Organisation-Unit` containing the original facility
of each offline report on submission and receipt polling. A mismatch with the
token's current assignment returns 403 before any queue write. This prevents
old offline reports from moving to a new facility after reassignment/token refresh.
The header remains optional for legacy integrations.

| Route | Behavior |
| --- | --- |
| `GET /api/v1/me` | Authenticated worker subject, display name, username and assigned organisation. Returns 403 if assignment is missing or invalid. |
| `PUT /api/v1/encounters/{uuid}` | 202 while QUEUED; 200 for a terminal receipt; 409 if the UUID is reused with changed data in the same organisation unit. |
| `GET /api/v1/encounters/{uuid}` | Receipt in the caller's organisation unit; 404 if absent. |
| `GET /api/v1/updates/manifest.jws` | Compact pre-signed manifest, `application/jose`. |
| `GET /api/v1/updates/{sha256}.zip` | Lowercase SHA-256-named ZIP archive. |
| `GET /actuator/health` | 200 with `{"status":"UP"}` or 503 with `{"status":"DOWN"}` if PostgreSQL is unavailable. |

Example request body:

```json
{"occurredAt":"2023-10-26T09:30:00+02:00","triage":"REFERRAL"}
```

Example receipt:

```json
{"encounterId":"a810227c-273f-486c-90c3-810fe71a2c33","state":"QUEUED"}
```

The timestamp requires an explicit offset and cannot be in the future. Triage is
`ROUTINE`, `REFERRAL`, or `URGENT_REFERRAL`. Generate and retain the UUID on Android;
retry the same ID and payload after a connection failure. Validation failures use
FastAPI's HTTP 422 JSON error format (the previous framework used HTTP 400).

## Queue and DHIS2 compatibility

PostgreSQL row leases and `FOR UPDATE SKIP LOCKED` allow replicas to share the queue.
Temporary failures retry with exponential backoff. Each retry uses the same DHIS2
event UID and `CREATE_AND_UPDATE` with synchronous Tracker import. The worker marks
FORWARDED only after a report confirms one created/updated event; structured
validation errors mark REJECTED. Authentication errors, outages and unrecognized
responses retry. Terminal states clear the encrypted payload but retain the receipt
and HMAC for deduplication. Logs exclude tokens, payloads and DHIS2 response bodies.

The `sync_event` schema, AES-256-GCM nonce/ciphertext layout, associated data,
HMAC-SHA256 receipt digest and deterministic event-UID algorithm match the previous
Java implementation. Existing tables and Flyway history are preserved. Stop the
Java service before switching; keep the database and encryption key unchanged.
Timestamp canonicalization preserves Java's nanosecond precision for existing
Android clients and receipt digests.

Configure the target DHIS2 program to accept the three triage values, assign its
capture permissions and organisation units, and verify delivery against staging.
Patient identifiers, notes, images and audio are outside the sync contract.

## Update publishing

Publish ZIPs under their lowercase SHA-256 digest plus `.zip`. Publish
`manifest.jws` last using atomic replacement. Sign outside this service using the
Android client's trusted key. Manifest payloads contain an increasing version and
a `packages` array with `kind`, `version`, `sha256`, and `sizeBytes`. Supported kinds
are `model` and `protocol`. A future mobile update installer must verify signatures, hashes, sizes and versions
before activation. That installer is not implemented in the current Android app. Mount update files read-only. Symlink downloads are rejected.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Optional integration tests require a **dedicated empty test database**, not production:

```powershell
$env:TEST_DATABASE_URL = 'postgresql://localhost:5432/communitycare_test'
$env:TEST_DATABASE_USER = 'communitycare'
$env:TEST_DATABASE_PASSWORD = 'your-test-password'
.\.venv\Scripts\python.exe -m pytest -q
```

Dependency versions validated for this conversion are recorded in `requirements.lock`.
Install that file for a reproducible runtime environment.

Deployment requires HTTPS termination, a small proxy request-body limit on sync,
encrypted PostgreSQL connections, managed secrets and shared update storage. The
API starts on port 8080 in the documented command to retain the Java default.

## Connected public DHIS2 demo

On November 22, 2026, a connection to DHIS2's public stable demo (2.43.2.1)
was configured in `.env`. The demo URL, API token, isolated CommunityCare event
program, stage, triage data element and test organisation unit are populated.
The token is never printed. The local encryption key is generated once and retained.

Live Tracker `importMode=VALIDATE` checks returned HTTP 200 / OK for all three
triage categories. No encounters were imported. See `DHIS2_CONNECTION_CHECK.json`
for the timestamped result, metadata IDs and remaining dependencies.

At the earlier public-demo setup stage, the API was not running: PostgreSQL and the OIDC login provider
still need configuration. These demo checks contact DHIS2 directly and do not
verify PostgreSQL queue persistence or full delivery from Android through the API.
`DHIS2_ORG_UNIT` in `.env` is the test organisation-unit UID; the future Android
access token's `dhis_org_unit` claim must match it for this test program.

The public demo is temporary, shared and periodically reset. After a reset,
regenerate the demo metadata and token and repeat validation:

```powershell
.\.venv\Scripts\python.exe -m app.setup_demo
```

This command uses the public demo's published login, creates only a separate
CommunityCare metadata namespace, grants its demo user data-capture access, saves
the token in `.env`, and performs validation-only requests. Existing valid demo
configuration and encryption keys are reused. It refuses to replace a connection
to a different DHIS2 host. Do not use this public server for real patient records.

References: [DHIS2 public demo](https://docs.dhis2.org/en/use/user-guides/dhis-core-version-242/additional-information/demo-server-live-package-and-database-design.html),
[Tracker validation mode](https://docs.dhis2.org/en/develop/using-the-api/dhis-core-version-243/tracker.html),
[data sharing](https://docs.dhis2.org/en/develop/using-the-api/dhis-core-version-242/sharing.html).

## Implementation guides

- [Frontend guide](docs/FRONTEND_GUIDE.md)
- [Backend guide](docs/BACKEND_GUIDE.md)
- [API contract](docs/API_CONTRACT.md)
- [Current submission status and limitations](docs/SUBMISSION_STATUS.md)


## Reporting-quality preview — 23 November 2026

Live nonclinical reporting-quality AI is implemented. See [release status](docs/REPORTING_QUALITY_RELEASE.md), [model card](docs/MODEL_CARD.md) and [data provenance](docs/DATA_SOURCES.md). The current owned DHIS2 pilot includes six engineering reports; the model is experimental and has no clinical validation.

Formatted documentation: [PDF](docs/CommunityCare_AI_Technical_Documentation.pdf) and [Word](docs/CommunityCare_AI_Technical_Documentation.docx). Body text is justified and all document text uses Century Gothic. [Public pitch deck](docs/CommunityCare_AI_Pitch_Deck.pdf). Reviewer credentials are supplied privately with the ITU application and are excluded from this repository.


## Restored app experience

Version 1.2 restores the original CommunityCare dashboard, quick actions, patient cards, five-tab navigation and sign-in styling while retaining hosted login, DHIS2 reporting and experimental reporting-quality AI. Patient registration, search, appointments and worker-entered notes use encrypted local storage. Patient identities and notes are not sent through the aggregate reporting contract. Messaging and clinical prediction remain unimplemented. See [feature and data boundaries](docs/RESTORED_APP.md).


## Original on phone account forms

Version 1.3 restores the original Create Account and Sign In fields directly on the phone, connected to the owned identity service through the Python backend. Live registration, login, refresh, pilot-only assignment and reporting analysis are verified. Checks: 49 backend tests and 22 Android tests, assembly and lint. [Account guide](docs/NATIVE_ACCOUNTS.md). Gmail recovery email still requires the owner’s app password.

## Four staff roles and original dashboard

See [STAFF_ROLES.md](docs/STAFF_ROLES.md) for the HSA, Nurse, Clinical Officer and Supervisor task matrix, test-pilot registration policy, operational approval, audit history and API contract.
