from datetime import datetime, timezone
from enum import StrEnum
import re
from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter, field_validator


class Triage(StrEnum):
    ROUTINE = "ROUTINE"
    REFERRAL = "REFERRAL"
    URGENT_REFERRAL = "URGENT_REFERRAL"


class Encounter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    occurredAt: str
    triage: Triage

    @field_validator("occurredAt", mode="before")
    @classmethod
    def require_iso_string(cls, value):
        if not isinstance(value, str):
            raise ValueError("occurredAt must be an ISO-8601 timestamp with an offset")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError("Expected ISO-8601 timestamp with up to nine fractional digits")
        timestamp = TypeAdapter(AwareDatetime).validate_python(value)
        if timestamp > datetime.now(timezone.utc):
            raise ValueError("occurredAt cannot be in the future")
        return value


def instant(value: str) -> str:
    """Match Java Instant.toString(), preserving existing receipt HMACs."""
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    fraction = re.search(r"\.(\d+)", value)
    nanos = int(fraction[1].ljust(9, "0")) if fraction else 0
    if nanos:
        width = 3 if nanos % 1_000_000 == 0 else 6 if nanos % 1000 == 0 else 9
        stamp += "." + f"{nanos:09d}"[:width]
    return stamp + "Z"
