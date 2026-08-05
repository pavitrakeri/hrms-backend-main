ALTER TABLE roles ADD COLUMN IF NOT EXISTS permissions JSONB DEFAULT '[]'::jsonb;

-- Default permissions for admin
UPDATE roles
SET permissions = '["manage_employees", "manage_roles", "manage_departments", "manage_leaves", "manage_payroll", "manage_policies", "manage_recruitment"]'::jsonb
WHERE name = 'admin';

-- Default permissions for hr
UPDATE roles
SET permissions = '["manage_employees", "manage_roles", "manage_departments", "manage_leaves", "manage_payroll", "manage_policies", "manage_recruitment"]'::jsonb
WHERE name = 'hr';

-- Default permissions for line_manager
UPDATE roles
SET permissions = '["manage_leaves", "manage_recruitment"]'::jsonb
WHERE name = 'line_manager';

-- Default permissions for cfo / finance
UPDATE roles
SET permissions = '["manage_payroll"]'::jsonb
WHERE name IN ('cfo', 'finance');
