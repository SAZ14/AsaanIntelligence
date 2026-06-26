-- =============================================================================
-- AsaanPay Central Server — DB Migration
-- Run in Supabase SQL Editor (Dashboard → SQL Editor → New Query)
-- Safe to re-run: uses IF NOT EXISTS / exception-caught constraint blocks
-- =============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 1 — New tables
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chains (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS store_twilio_numbers (
    id               SERIAL PRIMARY KEY,
    store_id         INTEGER NOT NULL UNIQUE REFERENCES stores(id) ON DELETE CASCADE,
    whatsapp_number  TEXT NOT NULL UNIQUE,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS revenue_connections (
    id         SERIAL PRIMARY KEY,
    store_id   INTEGER NOT NULL UNIQUE REFERENCES stores(id) ON DELETE CASCADE,
    data_dir   TEXT,
    db_path    TEXT,
    config     JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);


-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 2 — New columns on existing tables
-- ─────────────────────────────────────────────────────────────────────────────

-- stores: link to a chain (nullable — standalone restaurants stay NULL)
ALTER TABLE stores
    ADD COLUMN IF NOT EXISTS chain_id INTEGER REFERENCES chains(id);

-- user_sessions: remember which agent the user was last talking to
ALTER TABLE user_sessions
    ADD COLUMN IF NOT EXISTS active_agent TEXT;


-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 3 — Add store_id to customer community tables
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE venue_config        ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id);
ALTER TABLE community_members   ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id);
ALTER TABLE redeem_codes        ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id);
ALTER TABLE stamp_events        ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id);
ALTER TABLE deals               ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id);
ALTER TABLE onboarding_sessions ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id);
ALTER TABLE chat_sessions       ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id);
ALTER TABLE knowledge_base      ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id);


-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 4 — Backfill existing Sugar Rush data
--
-- Verify the store id first:  SELECT id, name FROM stores;
-- Replace 1 below if Sugar Rush has a different id.
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE venue_config        SET store_id = 1 WHERE store_id IS NULL;
UPDATE community_members   SET store_id = 1 WHERE store_id IS NULL;
UPDATE redeem_codes        SET store_id = 1 WHERE store_id IS NULL;
UPDATE stamp_events        SET store_id = 1 WHERE store_id IS NULL;
UPDATE deals               SET store_id = 1 WHERE store_id IS NULL;
UPDATE onboarding_sessions SET store_id = 1 WHERE store_id IS NULL;
UPDATE chat_sessions       SET store_id = 1 WHERE store_id IS NULL;
UPDATE knowledge_base      SET store_id = 1 WHERE store_id IS NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 5 — Unique constraints (skipped silently if already present)
-- ─────────────────────────────────────────────────────────────────────────────

DO $$ BEGIN
    ALTER TABLE venue_config ADD CONSTRAINT venue_config_store_id_key UNIQUE (store_id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE community_members ADD CONSTRAINT community_members_store_phone_key UNIQUE (store_id, phone);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE onboarding_sessions ADD CONSTRAINT onboarding_sessions_store_phone_key UNIQUE (store_id, phone);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE chat_sessions ADD CONSTRAINT chat_sessions_store_phone_key UNIQUE (store_id, phone);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;


-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 6 — Verify: shows all affected tables and their column counts
-- ─────────────────────────────────────────────────────────────────────────────

SELECT
    table_name,
    COUNT(*) AS column_count
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN (
      'chains', 'stores', 'store_twilio_numbers', 'revenue_connections',
      'user_sessions', 'venue_config', 'community_members',
      'redeem_codes', 'stamp_events', 'deals',
      'onboarding_sessions', 'chat_sessions', 'knowledge_base'
  )
GROUP BY table_name
ORDER BY table_name;
