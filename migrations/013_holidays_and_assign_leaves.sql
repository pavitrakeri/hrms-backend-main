-- Migration 013: Ensure holidays and notifications tables exist
CREATE TABLE IF NOT EXISTS holidays (
    id SERIAL PRIMARY KEY,
    date DATE UNIQUE NOT NULL,
    name TEXT NOT NULL
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'holidays' AND column_name = 'id' AND data_type IN ('integer', 'bigint', 'smallint') AND column_default IS NULL
    ) THEN
        CREATE SEQUENCE IF NOT EXISTS holidays_id_seq;
        ALTER TABLE holidays ALTER COLUMN id SET DEFAULT nextval('holidays_id_seq');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    payload JSONB,
    is_read BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications(user_id);
CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at DESC);
