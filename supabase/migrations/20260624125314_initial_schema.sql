-- 1. Create a function to automatically update 'updated_at' columns
CREATE OR REPLACE FUNCTION trigger_set_timestamp()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 2. Venue Config
CREATE TABLE venue_config (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    venue_name TEXT NOT NULL DEFAULT 'Sugar Rush',
    stamp_goal INTEGER NOT NULL DEFAULT 5,
    reward_text TEXT NOT NULL DEFAULT 'a free drink or dessert',
    winback_days INTEGER NOT NULL DEFAULT 5,
    code_expiry_days INTEGER NOT NULL DEFAULT 30,
    owner_phones TEXT[] NOT NULL DEFAULT '{}',
    qr_greeting TEXT NOT NULL DEFAULT 'Hi Sugar Rush! I''d like to join the community.',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Apply the updated_at trigger to venue_config
CREATE TRIGGER set_timestamp_venue_config
BEFORE UPDATE ON venue_config
FOR EACH ROW
EXECUTE PROCEDURE trigger_set_timestamp();

-- 3. Community Members
CREATE TABLE community_members (
    phone TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    stamps_current INTEGER NOT NULL DEFAULT 0,
    stamps_lifetime INTEGER NOT NULL DEFAULT 0,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    opted_in BOOLEAN NOT NULL DEFAULT TRUE,
    winback_sent_at TIMESTAMPTZ
);

-- 4. Redeem Codes
CREATE TABLE redeem_codes (
    code TEXT PRIMARY KEY,
    order_id TEXT NOT NULL DEFAULT '',
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    redeemed_at TIMESTAMPTZ,
    redeemed_by TEXT REFERENCES community_members(phone) ON DELETE SET NULL
);

-- 5. Stamp Events
CREATE TABLE stamp_events (
    id BIGSERIAL PRIMARY KEY,
    phone TEXT NOT NULL REFERENCES community_members(phone) ON DELETE CASCADE,
    code TEXT NOT NULL,
    stamp_number INTEGER NOT NULL,
    reward_issued BOOLEAN NOT NULL DEFAULT FALSE,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 6. Deals
CREATE TABLE deals (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

-- 7. Onboarding Sessions
-- (Intentionally left without a foreign key as users may not be full members yet)
CREATE TABLE onboarding_sessions (
    phone TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 8. Explicit Performance Indexes
CREATE INDEX idx_stamp_events_phone ON stamp_events(phone);
CREATE INDEX idx_redeem_codes_redeemed_by ON redeem_codes(redeemed_by);
CREATE INDEX idx_community_members_opted_in ON community_members(opted_in);

-- 9. Security (Row Level Security)
-- This locks down public access. Your backend's service_role key will automatically bypass this.
ALTER TABLE venue_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE community_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE redeem_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE stamp_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE deals ENABLE ROW LEVEL SECURITY;
ALTER TABLE onboarding_sessions ENABLE ROW LEVEL SECURITY;
