-- Add chat_sessions table for conversational memory
CREATE TABLE chat_sessions (
    phone TEXT PRIMARY KEY REFERENCES community_members(phone) ON DELETE CASCADE,
    history JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Apply the updated_at trigger
CREATE TRIGGER set_timestamp_chat_sessions
BEFORE UPDATE ON chat_sessions
FOR EACH ROW
EXECUTE PROCEDURE trigger_set_timestamp();

-- Enable RLS
ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY;
