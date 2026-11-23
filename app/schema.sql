CREATE TABLE IF NOT EXISTS sync_event (
    org_unit varchar(11) NOT NULL,
    encounter_id uuid NOT NULL,
    event_uid varchar(11) NOT NULL UNIQUE,
    payload bytea,
    payload_digest bytea NOT NULL,
    state varchar(16) NOT NULL DEFAULT 'QUEUED'
        CHECK (state IN ('QUEUED', 'FORWARDED', 'REJECTED')),
    attempts integer NOT NULL DEFAULT 0,
    next_attempt timestamptz NOT NULL DEFAULT now(),
    lease_id uuid,
    PRIMARY KEY (org_unit, encounter_id)
);
CREATE INDEX IF NOT EXISTS sync_event_ready ON sync_event (next_attempt) WHERE state='QUEUED';
