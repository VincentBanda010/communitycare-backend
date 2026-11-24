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

CREATE TABLE IF NOT EXISTS care_case (
    org_unit varchar(11) NOT NULL,
    case_id uuid NOT NULL,
    created_by varchar(255) NOT NULL,
    creator_role varchar(32) NOT NULL,
    assigned_to varchar(255),
    state varchar(40) NOT NULL CHECK(state IN ('AWAITING_NURSE','NURSE_REVIEW','AWAITING_CLINICAL_OFFICER','CLINICAL_REVIEW','FOLLOW_UP','CLOSED')),
    priority varchar(10) NOT NULL CHECK(priority IN ('ROUTINE','URGENT')),
    version integer NOT NULL DEFAULT 1,
    payload bytea NOT NULL,
    creation_digest bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(org_unit,case_id)
);
CREATE INDEX IF NOT EXISTS care_case_recent ON care_case(org_unit,updated_at DESC);
CREATE INDEX IF NOT EXISTS care_case_owner ON care_case(org_unit,created_by);
CREATE INDEX IF NOT EXISTS care_case_assignee ON care_case(org_unit,assigned_to);
CREATE TABLE IF NOT EXISTS care_case_history (
    org_unit varchar(11) NOT NULL,
    command_id uuid NOT NULL,
    case_id uuid NOT NULL,
    actor_id varchar(255) NOT NULL,
    actor_role varchar(32) NOT NULL,
    action varchar(40) NOT NULL,
    state varchar(40) NOT NULL,
    version integer NOT NULL,
    payload bytea NOT NULL,
    command_digest bytea NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(org_unit,command_id),
    UNIQUE(org_unit,case_id,version),
    FOREIGN KEY(org_unit,case_id) REFERENCES care_case(org_unit,case_id)
);
CREATE TABLE IF NOT EXISTS staff_role_audit (
    org_unit varchar(11) NOT NULL,
    request_id uuid NOT NULL,
    actor_id varchar(255) NOT NULL,
    target_id varchar(255) NOT NULL,
    payload bytea NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(org_unit,request_id)
);
