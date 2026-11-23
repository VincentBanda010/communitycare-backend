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

CREATE TABLE IF NOT EXISTS team_message (
    org_unit varchar(11) NOT NULL,
    message_id uuid NOT NULL,
    sender_id varchar(255) NOT NULL,
    payload bytea NOT NULL,
    payload_digest bytea NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (org_unit, message_id)
);
CREATE INDEX IF NOT EXISTS team_message_recent ON team_message(org_unit, sent_at DESC, message_id DESC);
CREATE INDEX IF NOT EXISTS team_message_sender ON team_message(org_unit, sender_id, sent_at DESC);
