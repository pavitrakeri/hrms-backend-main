CREATE TABLE IF NOT EXISTS company_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name TEXT DEFAULT 'Aimploy',
    office_start_time TIME DEFAULT '09:00:00',
    office_end_time TIME DEFAULT '18:00:00',
    weekend_days TEXT DEFAULT 'Saturday,Sunday',
    currency TEXT DEFAULT '',
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Seed initial default configuration if not present
INSERT INTO company_settings (company_name, office_start_time, office_end_time, weekend_days, currency)
SELECT 'Aimploy', '09:00:00', '18:00:00', 'Saturday,Sunday', ''
WHERE NOT EXISTS (SELECT 1 FROM company_settings);
