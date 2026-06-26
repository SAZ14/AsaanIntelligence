CREATE TABLE IF NOT EXISTS public.stores (
    id SERIAL PRIMARY KEY,
    name VARCHAR NOT NULL,
    location VARCHAR,
    category VARCHAR,
    instagram_handle VARCHAR,
    config JSON,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.store_locations (
    id SERIAL PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    address VARCHAR NOT NULL,
    city VARCHAR,
    area VARCHAR,
    is_primary VARCHAR DEFAULT 'false',
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.store_members (
    id SERIAL PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    whatsapp VARCHAR NOT NULL,
    role VARCHAR DEFAULT 'owner',
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    CONSTRAINT store_members_store_id_whatsapp_key UNIQUE (store_id, whatsapp)
);

CREATE TABLE IF NOT EXISTS public.user_sessions (
    whatsapp VARCHAR PRIMARY KEY,
    store_id INTEGER REFERENCES public.stores(id) ON DELETE SET NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.competitors (
    id SERIAL PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    name VARCHAR NOT NULL,
    category VARCHAR,
    instagram_handle VARCHAR,
    website VARCHAR,
    place_id VARCHAR,
    source VARCHAR DEFAULT 'seed',
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    CONSTRAINT competitors_store_id_name_key UNIQUE (store_id, name)
);

CREATE TABLE IF NOT EXISTS public.runs (
    id SERIAL PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    command VARCHAR NOT NULL,
    started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    status VARCHAR DEFAULT 'running',
    sources_ok JSON DEFAULT '[]'::json,
    sources_failed JSON DEFAULT '[]'::json,
    finding_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS public.findings (
    id SERIAL PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES public.runs(id) ON DELETE CASCADE,
    competitor_name VARCHAR NOT NULL,
    source_platform VARCHAR NOT NULL,
    update_type VARCHAR NOT NULL,
    content_text TEXT NOT NULL,
    rating DOUBLE PRECISION,
    post_date TIMESTAMP WITHOUT TIME ZONE,
    source_url VARCHAR,
    image_url VARCHAR,
    engagement JSON DEFAULT '{}'::json,
    collected_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    ai_summary TEXT,
    relevance_score INTEGER,
    content_hash VARCHAR NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS public.reports (
    id SERIAL PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES public.runs(id) ON DELETE CASCADE,
    command VARCHAR NOT NULL,
    report_text TEXT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

-- Indices for performance
CREATE INDEX IF NOT EXISTS idx_store_members_whatsapp ON public.store_members(whatsapp);
CREATE INDEX IF NOT EXISTS idx_runs_store_id ON public.runs(store_id);
CREATE INDEX IF NOT EXISTS idx_findings_store_id ON public.findings(store_id);
CREATE INDEX IF NOT EXISTS idx_findings_content_hash ON public.findings(content_hash);
CREATE INDEX IF NOT EXISTS idx_reports_store_id ON public.reports(store_id);

-- Enable RLS for all new tables
ALTER TABLE public.stores ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.store_locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.store_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.competitors ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.findings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reports ENABLE ROW LEVEL SECURITY;
