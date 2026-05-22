-- Keep HRMS as a single-company deployment for now.
ALTER TABLE users ADD COLUMN IF NOT EXISTS company TEXT DEFAULT 'aimploy';

UPDATE users
SET company = 'aimploy'
WHERE company IS DISTINCT FROM 'aimploy';

ALTER TABLE users ALTER COLUMN company SET DEFAULT 'aimploy';
