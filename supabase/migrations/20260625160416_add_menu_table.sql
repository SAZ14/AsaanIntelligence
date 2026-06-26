CREATE TABLE menu_items (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    cost NUMERIC,
    price NUMERIC NOT NULL,
    active BOOLEAN DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER set_timestamp_menu_items
BEFORE UPDATE ON menu_items
FOR EACH ROW
EXECUTE PROCEDURE trigger_set_timestamp();

ALTER TABLE menu_items ENABLE ROW LEVEL SECURITY;
