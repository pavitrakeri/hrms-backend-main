-- =====================================
-- HRMS DATABASE SCHEMA (Updated)
-- =====================================

-- Enable UUID & crypto extensions
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ===========================
-- ROLES
-- ===========================
CREATE TABLE IF NOT EXISTS roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL
);

-- Seed default roles
INSERT INTO roles (name)
VALUES ('admin'), ('employee'), ('line_manager'), ('hr'), ('cfo')
ON CONFLICT DO NOTHING;


-- ===========================
-- DEPARTMENTS
-- ===========================
CREATE TABLE IF NOT EXISTS departments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    manager_id UUID REFERENCES users(id),
    hr_id UUID REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);


-- ===========================
-- USERS (EMPLOYEES)
-- ===========================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role_id UUID REFERENCES roles(id),
    manager_id UUID REFERENCES users(id),
    department_id UUID REFERENCES departments(id),
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    company TEXT DEFAULT 'aimploy',
    status TEXT,
    office_location TEXT,
    designation TEXT,
    joining_date DATE,
    gender TEXT,
    date_of_birth DATE,
    marital_status TEXT,
    nationality TEXT,
    passport_number TEXT,
    emirates_id_number TEXT,
    uid_number TEXT,
    file_number TEXT,
    contract_type TEXT,
    labour_card_number TEXT,
    labour_card_expiry DATE,
    visa_sponsorship TEXT,
    residence_visa_expiry DATE,
    work_email TEXT,
    contact_number TEXT,
    personal_email TEXT,
    basic_salary NUMERIC,
    hra NUMERIC,
    mobile NUMERIC,
    transportation NUMERIC,
    other NUMERIC,
    total_salary NUMERIC,
    flight_ticket TEXT,
    wps_unique_id TEXT,
    wps TEXT,
    medical_insurance_category TEXT
);

ALTER TABLE users
ADD COLUMN IF NOT EXISTS company TEXT DEFAULT 'aimploy',
ADD COLUMN status TEXT,
ADD COLUMN office_location TEXT,
ADD COLUMN designation TEXT,
ADD COLUMN gender TEXT,
ADD COLUMN date_of_birth DATE,
ADD COLUMN marital_status TEXT,
ADD COLUMN nationality TEXT,
ADD COLUMN passport_number TEXT,
ADD COLUMN emirates_id_number TEXT,
ADD COLUMN uid_number TEXT,
ADD COLUMN file_number TEXT,
ADD COLUMN contract_type TEXT,
ADD COLUMN labour_card_number TEXT,
ADD COLUMN labour_card_expiry DATE,
ADD COLUMN visa_sponsorship TEXT,
ADD COLUMN residence_visa_expiry DATE,
ADD COLUMN work_email TEXT,
ADD COLUMN contact_number TEXT,
ADD COLUMN personal_email TEXT,
ADD COLUMN basic_salary NUMERIC,
ADD COLUMN hra NUMERIC,
ADD COLUMN mobile NUMERIC,
ADD COLUMN transportation NUMERIC,
ADD COLUMN other NUMERIC,
ADD COLUMN total_salary NUMERIC,
ADD COLUMN flight_ticket TEXT,
ADD COLUMN wps_unique_id TEXT,
ADD COLUMN wps TEXT,
ADD COLUMN medical_insurance_category TEXT;


-- ===========================
-- ATTENDANCE
-- ===========================
CREATE TABLE IF NOT EXISTS attendance (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    clock_in_at TIMESTAMP WITH TIME ZONE,
    clock_out_at TIMESTAMP WITH TIME ZONE,
    total_seconds INT,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    location TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);


-- ===========================
-- LEAVE TYPES
-- ===========================
CREATE TABLE IF NOT EXISTS leave_types (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    default_days INT NOT NULL
);

-- Seed leave types (removed "casual")
INSERT INTO leave_types (id, name, default_days) VALUES
    (1, 'sick', 0),
    (2, 'annual', 0),
    (3, 'unpaid', 0)
ON CONFLICT DO NOTHING;


-- ===========================
-- LEAVES
-- ===========================
CREATE TABLE IF NOT EXISTS leaves (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    leave_type_id INT REFERENCES leave_types(id),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    half_day BOOLEAN DEFAULT false,
    half_day_slot TEXT,
    reason TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    workflow JSONB,
    current_step TEXT
);


-- ===========================
-- LEAVE APPROVALS
-- ===========================
CREATE TABLE IF NOT EXISTS leave_approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    leave_id UUID REFERENCES leaves(id),
    approver_id UUID REFERENCES users(id),
    approver_role TEXT,
    decision TEXT DEFAULT 'pending',
    decided_at TIMESTAMP WITH TIME ZONE
);


-- ===========================
-- HOLIDAYS
-- ===========================
CREATE TABLE IF NOT EXISTS holidays (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date DATE UNIQUE NOT NULL,
    name TEXT NOT NULL
);


-- ===========================
-- RESIGNATIONS
-- ===========================
CREATE TABLE IF NOT EXISTS resignations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    reason TEXT NOT NULL,
    last_working_day DATE NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS resignation_approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resignation_id UUID REFERENCES resignations(id) ON DELETE CASCADE,
    approver_id UUID REFERENCES users(id),
    approver_role TEXT,
    decision TEXT DEFAULT 'pending',
    decided_at TIMESTAMP WITH TIME ZONE
);


-- ===========================
-- POLICIES
-- ===========================
CREATE TABLE IF NOT EXISTS policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    description TEXT,
    file_url TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS policy_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id UUID REFERENCES policies(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    acknowledged BOOLEAN DEFAULT false,
    acknowledged_at TIMESTAMP WITH TIME ZONE
);


-- ===========================
-- RECRUITMENT REQUESTS
-- ===========================
CREATE TABLE IF NOT EXISTS recruitments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    manager_id UUID REFERENCES users(id),
    position TEXT NOT NULL,
    department_id UUID REFERENCES departments(id),
    budget NUMERIC,
    job_description TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recruitment_approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recruitment_id UUID REFERENCES recruitments(id) ON DELETE CASCADE,
    approver_id UUID REFERENCES users(id),
    approver_role TEXT NOT NULL,
    decision TEXT DEFAULT 'pending',
    decided_at TIMESTAMP WITH TIME ZONE
);


-- ===========================
-- DOCUMENTS & ACKNOWLEDGEMENTS (LEGACY)
-- ===========================
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_acknowledgements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID REFERENCES documents(id),
    user_id UUID REFERENCES users(id),
    acknowledged_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);


-- ===========================
-- NOTIFICATIONS
-- ===========================
CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    type TEXT NOT NULL,
    payload JSONB,
    is_read BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);


-- ===========================
-- SEED DEFAULT USERS
-- ===========================
INSERT INTO users (email, full_name, password_hash, role_id, is_active)
VALUES (
    'system-admin@aimploy.com',
    'Admin User',
    '$2b$12$F0c6vE2FqQF2tMzN2y86vOE.7h5iJ3zJ0cYGlPdgHbK5g34abnsBa',
    (SELECT id FROM roles WHERE name='admin'),
    true
)
ON CONFLICT DO NOTHING;

-- 1️⃣ Add employment_status & joining_date to users
ALTER TABLE users
ADD COLUMN IF NOT EXISTS employment_status TEXT DEFAULT 'probation',  -- probation | permanent | temporary
ADD COLUMN IF NOT EXISTS joining_date DATE;

-- 2️⃣ Add medical_document_url to leaves
ALTER TABLE leaves
ADD COLUMN IF NOT EXISTS medical_document_url TEXT;

-- 3️⃣ Create leave_balance table
CREATE TABLE IF NOT EXISTS leave_balance (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    leave_type_id INT REFERENCES leave_types(id),
    year INT DEFAULT EXTRACT(YEAR FROM now()),
    total_entitled NUMERIC DEFAULT 0,        -- total allocated for that year
    used_days NUMERIC DEFAULT 0,
    carried_forward NUMERIC DEFAULT 0,
    remaining NUMERIC GENERATED ALWAYS AS (total_entitled + carried_forward - used_days) STORED,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT now(),
    UNIQUE (user_id, leave_type_id, year)
);

UPDATE users
SET employment_status = 'permanent',
    joining_date = '2024-01-01'
WHERE email = 'employee@aimploy.com';


INSERT INTO users (
    id,
    email,
    full_name,
    password_hash,
    role_id,
    is_active,
    created_at,
    status,
    office_location,
    designation,
    joining_date,
    employment_status
)
VALUES (
    gen_random_uuid(),
    'admin@aimploy.com',
    'System Admin',
    '$2b$12$igB/s2Pt8hvUkcwF31sNG.3tHCIW6d.Bnxp7NtA9bxnsRhmOq8FI2',
    1,
    true,
    now(),
    'active',
    'HQ - Abu Dhabi',
    'Administrator',
    '2025-01-01',
    'permanent'
);


UPDATE users
SET employment_status = 'permanent'
WHERE email = 'manager@aimploy.com';


-- ===============================================
-- STEP 1: Backup current roles table
-- ===============================================
CREATE TABLE IF NOT EXISTS roles_backup AS SELECT * FROM roles;

-- ===============================================
-- STEP 2: Drop foreign key constraint from users
-- ===============================================
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_id_fkey;

-- ===============================================
-- STEP 3: Drop old roles.id column and recreate as UUID
-- ===============================================
ALTER TABLE roles DROP COLUMN id;

ALTER TABLE roles
ADD COLUMN id UUID PRIMARY KEY DEFAULT gen_random_uuid();

-- ===============================================
-- STEP 4: Recreate the unique index on role name
-- ===============================================
CREATE UNIQUE INDEX IF NOT EXISTS roles_name_key ON public.roles (name);

-- ===============================================
-- STEP 5: Modify users.role_id to UUID
-- ===============================================
-- ⚠️ WARNING: this cast will fail if existing role_id values are not valid UUIDs.
-- If you had only integer role_ids (1,2,3...), this step resets them to NULL temporarily.

ALTER TABLE users
ALTER COLUMN role_id DROP DEFAULT,
ALTER COLUMN role_id TYPE UUID
USING NULL;  -- set temporarily NULL until we reassign below

-- ===============================================
-- STEP 6: Recreate the foreign key constraint
-- ===============================================
ALTER TABLE users
ADD CONSTRAINT users_role_id_fkey FOREIGN KEY (role_id) REFERENCES roles(id);

-- ===============================================
-- STEP 7: Reinsert core roles
-- ===============================================
INSERT INTO roles (id, name)
SELECT gen_random_uuid(), role_name
FROM (
  VALUES
    ('admin'),
    ('hr'),
    ('manager'),
    ('employee')
) AS v(role_name)
WHERE NOT EXISTS (
  SELECT 1 FROM roles r WHERE LOWER(r.name) = LOWER(v.role_name)
);


-- ===============================================
-- STEP 8: Verify structure
-- ===============================================
-- SELECT * FROM roles;
-- \d roles;
-- \d users;

-- ===============================================
-- ✅ Done
-- ===============================================


UPDATE users
SET role_id = (SELECT id FROM roles WHERE LOWER(name) = 'admin')
WHERE LOWER(email) LIKE '%admin%' OR LOWER(designation) LIKE '%admin%';


ALTER TABLE roles
ADD COLUMN description TEXT;


CREATE TABLE IF NOT EXISTS payroll_cycles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    month DATE NOT NULL, -- e.g., 2025-10-01
    status TEXT NOT NULL DEFAULT 'draft', -- draft / processing / approved / paid
    total_gross NUMERIC DEFAULT 0,
    total_net NUMERIC DEFAULT 0,
    created_by UUID REFERENCES users(id),
    approved_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    approved_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS payroll_cycles_month_key ON payroll_cycles(month);

CREATE TABLE IF NOT EXISTS payroll_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payroll_cycle_id UUID REFERENCES payroll_cycles(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id),
    basic NUMERIC DEFAULT 0,
    allowances JSONB DEFAULT '{}', -- e.g. {"hra": 2000, "transport": 500}
    reimbursements_total NUMERIC DEFAULT 0,
    deductions JSONB DEFAULT '{}', -- e.g. {"unpaid_leave": 250, "loan": 300}
    total_deductions NUMERIC DEFAULT 0,
    gross_pay NUMERIC DEFAULT 0,
    net_pay NUMERIC DEFAULT 0,
    gratuity_accrued NUMERIC DEFAULT 0,
    working_days INTEGER DEFAULT 30,
    unpaid_leave_days INTEGER DEFAULT 0,
    sick_days INTEGER DEFAULT 0,
    notes TEXT,
    payslip_url TEXT, -- Supabase storage link
    status TEXT DEFAULT 'pending', -- pending / approved / paid
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(payroll_cycle_id, user_id)
);

CREATE INDEX IF NOT EXISTS payroll_items_user_id_idx ON payroll_items(user_id);
CREATE INDEX IF NOT EXISTS payroll_items_cycle_idx ON payroll_items(payroll_cycle_id);


CREATE TABLE IF NOT EXISTS reimbursements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    category TEXT NOT NULL, -- e.g., "Travel"
    subcategory TEXT,       -- e.g., "Taxi/Uber"
    amount NUMERIC NOT NULL CHECK (amount > 0),
    description TEXT,
    expense_date DATE NOT NULL,
    supporting_docs JSONB DEFAULT '[]', -- list of Supabase file paths
    status TEXT DEFAULT 'pending', -- pending / manager_approved / finance_approved / cfo_approved / paid / rejected
    created_at TIMESTAMPTZ DEFAULT now(),
    decided_by UUID REFERENCES users(id),
    decided_at TIMESTAMPTZ,
    paid_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS reimbursements_user_id_idx ON reimbursements(user_id);
CREATE INDEX IF NOT EXISTS reimbursements_status_idx ON reimbursements(status);


CREATE TABLE IF NOT EXISTS change_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    request_type TEXT NOT NULL, -- e.g., "salary_update", "personal_info_update"
    old_value JSONB, -- old data snapshot
    new_value JSONB, -- requested change
    reason TEXT,
    status TEXT DEFAULT 'pending', -- pending / approved / rejected
    created_at TIMESTAMPTZ DEFAULT now(),
    decided_by UUID REFERENCES users(id),
    decided_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS change_requests_user_id_idx ON change_requests(user_id);
CREATE INDEX IF NOT EXISTS change_requests_status_idx ON change_requests(status);


CREATE TABLE reimbursement_approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reimbursement_id uuid REFERENCES reimbursements(id) ON DELETE CASCADE,
    approver_id uuid REFERENCES users(id),
    approver_role text,   -- manager / finance / cfo
    decision text DEFAULT 'pending',  -- pending / approved / rejected / query
    comment text,
    decided_at timestamptz
);

CREATE INDEX IF NOT EXISTS reimbursement_approvals_reim_idx ON reimbursement_approvals(reimbursement_id);
CREATE INDEX IF NOT EXISTS reimbursement_approvals_approver_idx ON reimbursement_approvals(approver_id);


CREATE TABLE payroll_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id uuid REFERENCES users(id),
    request_type text NOT NULL, -- 'advance', 'certificate', 'query', 'schedule_change'
    amount numeric,
    purpose text,
    query_type text,
    reason text,
    description text,
    requested_date date,
    attachments text[],
    status text DEFAULT 'pending',
    current_approver_role text,
    resolution_notes text,
    created_at timestamp DEFAULT now(),
    updated_at timestamp DEFAULT now()
);

CREATE TABLE IF NOT EXISTS employee_payroll_setup (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id uuid REFERENCES users(id) ON DELETE CASCADE,
    employee_email text NOT NULL,
    basic_salary numeric NOT NULL,
    hra numeric DEFAULT 0,
    allowances numeric DEFAULT 0,
    other_benefits numeric DEFAULT 0,
    gross_monthly numeric NOT NULL,
    gross_annual numeric NOT NULL,
    payment_mode text,
    bank_account_number text,
    bank_name text,
    iban_number text,
    remarks text,
    created_by uuid REFERENCES users(id),
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    UNIQUE(employee_id)
);


CREATE TABLE payroll_approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payroll_request_id uuid REFERENCES payroll_requests(id),
    approver_id uuid REFERENCES users(id),
    approver_role text,
    decision text DEFAULT 'pending', -- 'approved' | 'rejected'
    comment text,
    decided_at timestamp
);


CREATE TABLE IF NOT EXISTS employee_payroll_setup (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id uuid REFERENCES users(id) ON DELETE CASCADE,
    employee_email text NOT NULL,
    basic_salary numeric NOT NULL,
    hra numeric DEFAULT 0,
    allowances numeric DEFAULT 0,
    other_benefits numeric DEFAULT 0,
    gross_monthly numeric NOT NULL,
    gross_annual numeric NOT NULL,
    payment_mode text,
    bank_account_number text,
    bank_name text,
    iban_number text,
    remarks text,
    created_by uuid REFERENCES users(id),
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    UNIQUE(employee_id)
);


ALTER TABLE reimbursements
ADD COLUMN subcategory TEXT;


-- ===========================
-- PROJECTS & TASKS
-- ===========================
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    created_by UUID NOT NULL REFERENCES users(id),
    department_id UUID REFERENCES departments(id),
    start_date DATE,
    deadline DATE,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS project_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    assignee_id UUID REFERENCES users(id),
    created_by UUID NOT NULL REFERENCES users(id),
    due_date DATE,
    status TEXT NOT NULL DEFAULT 'todo',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
A L T E R   T A B L E   t a s k s   A D D   C O L U M N   I F   N O T   E X I S T S   s t a r t _ d a t e   D A T E ,   A D D   C O L U M N   I F   N O T   E X I S T S   t i m e r _ s t a r t e d _ a t   T I M E S T A M P T Z ,   A D D   C O L U M N   I F   N O T   E X I S T S   t i m e _ s p e n t _ s e c o n d s   I N T   D E F A U L T   0 ;  
 