"""Configure the public DHIS2 demo and validate synthetic data without importing it.

Run from the backend root: python -m app.setup_demo
Creates isolated demo metadata and a demo access token. No encounter is saved.
"""
import base64
import json
import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DEMO_HOST = "play.im.dhis2.org"
TRIAGE_VALUES = ("ROUTINE", "REFERRAL", "URGENT_REFERRAL")


def uid():
    return secrets.choice(string.ascii_letters) + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(10)
    )


def build_metadata(namespace, org, category_combo):
    program, stage, element, stage_element = (uid() for _ in range(4))
    label = f"CommunityCare AI Demo {namespace}"
    return {
        "dataElements": [{
            "id": element, "name": label + " triage", "shortName": "CCAI triage " + namespace,
            "code": "CCAI_TRIAGE_" + namespace, "valueType": "TEXT", "domainType": "TRACKER",
            "aggregationType": "NONE", "categoryCombo": {"id": category_combo},
        }],
        "programs": [{
            "id": program, "name": label, "shortName": "CCAI demo " + namespace,
            "code": "CCAI_DEMO_" + namespace, "programType": "WITHOUT_REGISTRATION",
            "organisationUnits": [{"id": org}], "categoryCombo": {"id": category_combo},
            "programStages": [{"id": stage}],
        }],
        "programStages": [{
            "id": stage, "name": label + " assessment", "code": "CCAI_STAGE_" + namespace,
            "program": {"id": program}, "minDaysFromStart": 0, "repeatable": False,
            "programStageDataElements": [{
                "id": stage_element, "dataElement": {"id": element},
                "compulsory": True, "displayInReports": True, "sortOrder": 1,
            }],
        }],
    }, {"DHIS2_PROGRAM": program, "DHIS2_STAGE": stage, "DHIS2_TRIAGE_ELEMENT": element,
        "DHIS2_ORG_UNIT": org, "DHIS2_DEMO_NAMESPACE": namespace}


def event_payload(mapping, triage):
    return {"events": [{
        "event": uid(), "program": mapping["DHIS2_PROGRAM"],
        "programStage": mapping["DHIS2_STAGE"], "orgUnit": mapping["DHIS2_ORG_UNIT"],
        "occurredAt": datetime.now(timezone.utc).isoformat(), "status": "COMPLETED",
        "dataValues": [{"dataElement": mapping["DHIS2_TRIAGE_ELEMENT"], "value": triage}],
    }]}


def save_env(path, updates):
    """Preserve unrelated settings and an existing encryption key."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else [
        "# CommunityCare AI — public demo connection (synthetic test data only)",
        "# PostgreSQL and OIDC still need to be configured before starting the API.",
        "DATABASE_URL=postgresql://localhost:5432/communitycare", "DATABASE_USER=communitycare",
        "DATABASE_PASSWORD=", "OIDC_ISSUER=", "OIDC_AUDIENCE=communitycare-api",
        "UPDATE_DIRECTORY=./updates",
    ]
    existing = dotenv_values(path) if path.exists() else {}
    updates = dict(updates)
    if not existing.get("SYNC_ENCRYPTION_KEY"):
        updates["SYNC_ENCRYPTION_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
    remaining = dict(updates)
    result = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            if key in remaining:
                result.append(f"{key}={remaining.pop(key)}")
        else:
            result.append(line)
    result.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def report_errors(body):
    if not isinstance(body, dict):
        return True
    validation = body.get("validationReport") or {}
    return bool(isinstance(validation, dict) and validation.get("errorReports"))


def resolve_demo(client):
    # No credentials are sent while following the public demo's redirects.
    response = client.get("https://play.dhis2.org/demo/", follow_redirects=True)
    response.raise_for_status()
    parts = urlsplit(str(response.url))
    if parts.scheme != "https" or parts.hostname != PUBLIC_DEMO_HOST:
        raise ValueError("Unexpected demo host; no credentials were sent")
    instance = parts.path.strip("/").split("/")[0]
    if not instance or not instance.startswith("stable-"):
        raise ValueError("Could not resolve the stable DHIS2 demo instance")
    return f"https://{PUBLIC_DEMO_HOST}/{instance}"


def configure(root=ROOT):
    env_path = root / ".env"
    existing = dotenv_values(env_path) if env_path.exists() else {}
    configured_host = urlsplit(existing.get("DHIS2_URL") or "").hostname
    if configured_host and configured_host not in (PUBLIC_DEMO_HOST, "dhis.example.com"):
        raise ValueError("Refusing to overwrite a non-demo DHIS2 connection")
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        base = resolve_demo(client)
        auth = ("admin", "district")  # Published credentials for this public demo only.

        def get(path, **params):
            response = client.get(base + path, params=params, auth=auth)
            response.raise_for_status()
            return response.json()

        actor = get("/api/me", fields="id")["id"]
        version = get("/api/system/info")["version"]
        reusable = existing.get("DHIS2_URL") == base and all(existing.get(n) for n in (
            "DHIS2_PROGRAM", "DHIS2_STAGE", "DHIS2_TRIAGE_ELEMENT", "DHIS2_ORG_UNIT",
        ))
        if reusable:
            check = client.get(base + "/api/programs/" + existing["DHIS2_PROGRAM"],
                               params={"fields": "id,code"}, auth=auth)
            reusable = check.status_code == 200 and check.json().get("code") == "CCAI_DEMO_" + existing.get("DHIS2_DEMO_NAMESPACE", "")
        if reusable:
            mapping = {name: existing[name] for name in (
                "DHIS2_PROGRAM", "DHIS2_STAGE", "DHIS2_TRIAGE_ELEMENT", "DHIS2_ORG_UNIT",
                "DHIS2_DEMO_NAMESPACE",
            )}
        else:
            orgs = get("/api/organisationUnits", fields="id,displayName", filter="level:eq:4", pageSize=1)
            combos = get("/api/categoryCombos", fields="id,name", filter="name:eq:default")
            org = orgs["organisationUnits"][0]["id"]
            combo = combos["categoryCombos"][0]["id"]
            metadata, mapping = build_metadata(secrets.token_hex(4), org, combo)
            response = client.post(base + "/api/metadata", params={"importStrategy": "CREATE"},
                                   auth=auth, json=metadata)
            response.raise_for_status()
            result = response.json()
            if result.get("status") not in ("OK", "SUCCESS"):
                # Metadata contain no patient data or credentials.
                raise ValueError("Demo metadata import failed: " + json.dumps(result)[:3000])
        # Data capture permissions are separate from metadata ownership in DHIS2.
        # Touch only the two objects created under this local demo namespace.
        for kind, object_id, expected_code in (
            ("program", mapping["DHIS2_PROGRAM"], "CCAI_DEMO_" + mapping["DHIS2_DEMO_NAMESPACE"]),
            ("programStage", mapping["DHIS2_STAGE"], "CCAI_STAGE_" + mapping["DHIS2_DEMO_NAMESPACE"]),
        ):
            record = get(f"/api/{kind}s/{object_id}", fields="id,code")
            if record.get("code") != expected_code:
                raise ValueError("Refusing to change sharing on an unrelated demo object")
            response = client.post(
                base + "/api/sharing", params={"type": kind, "id": object_id}, auth=auth,
                json={"object": {"publicAccess": "--------", "externalAccess": False,
                                 "user": {"id": actor}, "userGroupAccesses": [],
                                 "userAccesses": [{"id": actor, "access": "rwrw----"}]}},
            )
            response.raise_for_status()
        token = existing.get("DHIS2_TOKEN") if reusable else None
        if token:
            response = client.get(base + "/api/me", params={"fields": "id"},
                                  headers={"Authorization": "ApiToken " + token})
            if response.status_code != 200:
                token = None
        if not token:
            response = client.post(base + "/api/apiToken", auth=auth, json={})
            response.raise_for_status()
            token = response.json()["response"]["key"]
        # Persist even if a later validation fails so the same metadata/token can
        # be repaired and reused rather than creating duplicate demo resources.
        save_env(env_path, {**mapping, "DHIS2_URL": base, "DHIS2_TOKEN": token})
        validations = []
        for triage in TRIAGE_VALUES:
            response = client.post(
                base + "/api/tracker", headers={"Authorization": "ApiToken " + token},
                params={"async": "false", "importStrategy": "CREATE_AND_UPDATE",
                        "reportMode": "FULL", "importMode": "VALIDATE"},
                json=event_payload(mapping, triage),
            )
            response.raise_for_status()
            report = response.json()
            if report_errors(report) or report.get("status") not in ("OK", "WARNING"):
                raise ValueError("Tracker validation failed: " + json.dumps(report)[:3000])
            validations.append({"triage": triage, "http_status": response.status_code,
                                "status": report.get("status"), "validation_only": True})
        summary = {"server": base, "version": version, "authentication": "ApiToken verified",
                   "mapping": mapping, "validations": validations, "encounters_saved": 0,
                   "checked_at": datetime.now(timezone.utc).isoformat(),
                   "pending": ["PostgreSQL connection", "OIDC login provider", "Full queue delivery test"],
                   "note": "Public demo resets periodically; rerun setup after a reset."}
        (root / "DHIS2_CONNECTION_CHECK.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))
        return summary


if __name__ == "__main__":
    try:
        configure()
    except httpx.HTTPStatusError as error:
        detail = ""
        if error.request.url.path.endswith(("/api/metadata", "/api/tracker")):
            detail = ": " + error.response.text[:4000]
        raise SystemExit(f"DHIS2 {error.request.url.path} returned HTTP {error.response.status_code}; setup not verified{detail}") from None
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
        # Token-bearing responses and headers must never be printed.
        raise SystemExit(str(error) if isinstance(error, ValueError) else "DHIS2 setup failed; check connectivity and demo availability") from None
