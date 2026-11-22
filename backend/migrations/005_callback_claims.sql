ALTER TABLE family_contacts ADD COLUMN IF NOT EXISTS appointment int NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS callback_claims (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    contact_id bigint NOT NULL REFERENCES family_contacts,
    appointment int NOT NULL CHECK (appointment >= 0),
    claim_key text NOT NULL UNIQUE CHECK (length(btrim(claim_key)) BETWEEN 8 AND 64),
    claimed_by bigint NOT NULL REFERENCES staff,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','done','released','expired','superseded')),
    claimed_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL CHECK (expires_at > claimed_at),
    finished_at timestamptz,
    attempt_id bigint REFERENCES family_contact_attempts,
    CHECK ((status = 'active') = (finished_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_callback_claim ON callback_claims(contact_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS callback_claim_history ON callback_claims(contact_id, id DESC);
