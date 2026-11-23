import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

UID = re.compile(r"[A-Za-z][A-Za-z0-9]{10}")


def https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Expected an HTTPS base URL without credentials, query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    database_url: str
    database_user: str
    database_password: str
    issuer: str
    audience: str
    key: bytes
    dhis_url: str
    dhis_token: str
    program: str
    stage: str
    triage_element: str
    update_directory: Path

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv
        load_dotenv()

        def required(name):
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} must be configured")
            return value

        key = base64.b64decode(required("SYNC_ENCRYPTION_KEY"), validate=True)
        if len(key) != 32:
            raise ValueError("SYNC_ENCRYPTION_KEY must decode to 32 bytes")
        metadata = [required(n) for n in ("DHIS2_PROGRAM", "DHIS2_STAGE", "DHIS2_TRIAGE_ELEMENT")]
        if not all(UID.fullmatch(value) for value in metadata):
            raise ValueError("DHIS2 metadata IDs must be 11-character UIDs")
        database_url = os.getenv("DATABASE_URL", "postgresql://localhost:5432/communitycare")
        database_url = database_url.removeprefix("jdbc:")
        if urlsplit(database_url).scheme not in ("postgres", "postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return cls(
            database_url, os.getenv("DATABASE_USER", "communitycare"),
            required("DATABASE_PASSWORD"), required("OIDC_ISSUER"), required("OIDC_AUDIENCE"),
            key, https_url(required("DHIS2_URL")), required("DHIS2_TOKEN"), *metadata,
            Path(os.getenv("UPDATE_DIRECTORY", "./updates")).absolute(),
        )
