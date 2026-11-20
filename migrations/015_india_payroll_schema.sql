-- ==============================================================================
-- Migration: 015_india_payroll_schema.sql
-- Description: Core Schema for Aimploy HRMS India Statutory Payroll Engine
-- Statutory Scope: EPF, EPS, ESIC, State PT, Payment of Gratuity, TDS Sec 192,
--                  EPFO ECR text spec, Form 24Q e-TDS, and Bank NEFT batch exports.
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 1. Extend USERS Table with Indian Statutory & Identification Fields
-- ------------------------------------------------------------------------------
ALTER TABLE users 
    ADD COLUMN IF NOT EXISTS pan_number VARCHAR(10),
    ADD COLUMN IF NOT EXISTS uan_number VARCHAR(12),
    ADD COLUMN IF NOT EXISTS aadhaar_number VARCHAR(12),
    ADD COLUMN IF NOT EXISTS esic_ip_number VARCHAR(10),
    ADD COLUMN IF NOT EXISTS father_or_spouse_name TEXT,
    ADD COLUMN IF NOT EXISTS date_of_exit DATE,
    ADD COLUMN IF NOT EXISTS exit_reason_code TEXT;

CREATE INDEX IF NOT EXISTS idx_users_pan ON users(pan_number);
CREATE INDEX IF NOT EXISTS idx_users_uan ON users(uan_number);

-- ------------------------------------------------------------------------------
-- 2. COMPANY STATUTORY DETAILS (Corporate Legal & Statutory Master)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS company_statutory_details (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name TEXT NOT NULL,
    tan_number VARCHAR(10) NOT NULL,              -- Tax Deduction Account Number (e.g. BLRA12345C)
    pan_number VARCHAR(10) NOT NULL,              -- Corporate Permanent Account Number
    epfo_establishment_code VARCHAR(15) NOT NULL, -- 15-char EPFO Establishment ID (e.g. BGBNG0012345000)
    esic_establishment_code VARCHAR(17),          -- 17-digit ESIC Code
    is_esic_applicable BOOLEAN NOT NULL DEFAULT false, -- Establishment gate (10+ workers)
    pt_rc_number TEXT,                            -- Professional Tax Registration Certificate No.
    pt_ec_number TEXT,                            -- Professional Tax Enrolment Certificate No.
    registered_address TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------------------
-- 3. PROFESSIONAL TAX STATE SLABS (State-Level Statutory Rules)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pt_state_slabs (
    id SERIAL PRIMARY KEY,
    state_code VARCHAR(5) NOT NULL,               -- KA, MH, TS, TN, DL, etc.
    state_name TEXT NOT NULL,
    effective_from DATE NOT NULL DEFAULT '2024-04-01',
    min_monthly_gross NUMERIC(12,2) NOT NULL DEFAULT 0,
    max_monthly_gross NUMERIC(12,2),              -- NULL indicates open-ended top slab
    pt_amount NUMERIC(8,2) NOT NULL DEFAULT 0,
    special_feb_amount NUMERIC(8,2),              -- e.g. MH ₹300 in Feb instead of ₹200
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pt_slabs_lookup ON pt_state_slabs(state_code, effective_from);

-- ------------------------------------------------------------------------------
-- 4. EMPLOYEE SALARY STRUCTURES (Contractual CTC & Statutory Switches)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS employee_salary_structures (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    effective_from DATE NOT NULL,
    effective_to DATE,
    is_active BOOLEAN NOT NULL DEFAULT true,
    
    -- Earnings Components (Monthly INR)
    base_currency VARCHAR(5) NOT NULL DEFAULT 'INR',
    annual_ctc NUMERIC(12,2) NOT NULL CHECK (annual_ctc >= 0),
    monthly_basic_pay NUMERIC(12,2) NOT NULL CHECK (monthly_basic_pay >= 0),
    monthly_hra NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (monthly_hra >= 0),
    monthly_special_allowance NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (monthly_special_allowance >= 0),
    monthly_da NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (monthly_da >= 0),
    
    -- Statutory Rule Switches
    is_epf_eligible BOOLEAN NOT NULL DEFAULT true,
    epf_wage_cap_applied BOOLEAN NOT NULL DEFAULT true, -- Enforce statutory ₹15,000 wage ceiling
    vpf_percentage NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (vpf_percentage >= 0 AND vpf_percentage <= 100),
    is_esi_eligible BOOLEAN NOT NULL DEFAULT false,     -- Auto-resolved based on Gross <= ₹21,000
    pt_state_code VARCHAR(5) NOT NULL DEFAULT 'KA',
    tax_regime VARCHAR(10) NOT NULL DEFAULT 'NEW' CHECK (tax_regime IN ('NEW', 'OLD')),
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exactly one active structure per employee
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_salary_structure 
ON employee_salary_structures (employee_id) 
WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_salary_struct_emp ON employee_salary_structures(employee_id);

-- ------------------------------------------------------------------------------
-- 5. EMPLOYEE TAX DECLARATIONS (Old Regime Deductions & Investment Proofs)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS employee_tax_declarations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    financial_year VARCHAR(10) NOT NULL,          -- e.g. '2026-2027'
    regime VARCHAR(10) NOT NULL DEFAULT 'NEW' CHECK (regime IN ('NEW', 'OLD')),
    section_80c_amount NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (section_80c_amount >= 0),
    section_80d_amount NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (section_80d_amount >= 0),
    annual_rent_paid NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (annual_rent_paid >= 0),
    home_loan_interest NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (home_loan_interest >= 0),
    nps_80ccd_1b NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (nps_80ccd_1b >= 0),
    is_verified BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(employee_id, financial_year)
);

CREATE INDEX IF NOT EXISTS idx_tax_declarations_emp ON employee_tax_declarations(employee_id, financial_year);

-- ------------------------------------------------------------------------------
-- 6. EMPLOYEE BANK DETAILS (Indian Banking & IFSC Validation)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS employee_bank_details (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_method VARCHAR(20) NOT NULL DEFAULT 'BANK_TRANSFER' CHECK (payment_method IN ('BANK_TRANSFER', 'CHEQUE', 'CASH')),
    account_holder_name TEXT NOT NULL,
    bank_name TEXT NOT NULL,
    account_number TEXT NOT NULL,
    ifsc_code VARCHAR(11) NOT NULL,               -- 11-char Indian IFSC Code (e.g. HDFC0000240)
    is_primary BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bank_details_emp ON employee_bank_details(employee_id);

-- ------------------------------------------------------------------------------
-- 7. EMPLOYEE SALARY ADVANCES (Loan Repayment & EMI Schedules)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS employee_salary_advances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    principal_amount NUMERIC(12,2) NOT NULL CHECK (principal_amount > 0),
    monthly_emi_amount NUMERIC(12,2) NOT NULL CHECK (monthly_emi_amount > 0),
    total_installments INTEGER NOT NULL CHECK (total_installments > 0),
    paid_installments INTEGER NOT NULL DEFAULT 0 CHECK (paid_installments >= 0),
    remaining_balance NUMERIC(12,2) NOT NULL CHECK (remaining_balance >= 0),
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'COMPLETED', 'CANCELLED', 'PAUSED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_salary_advances_emp ON employee_salary_advances(employee_id, status);

-- ------------------------------------------------------------------------------
-- 8. PAYROLL CYCLES (Monthly Batches & State Machine)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll_cycles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_name TEXT NOT NULL,                     -- e.g. "November 2026 Payroll"
    cycle_month INTEGER NOT NULL CHECK (cycle_month >= 1 AND cycle_month <= 12),
    cycle_year INTEGER NOT NULL CHECK (cycle_year >= 2020),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    cutoff_date DATE NOT NULL,
    cycle_type VARCHAR(20) NOT NULL DEFAULT 'REGULAR' CHECK (cycle_type IN ('REGULAR', 'OFF_CYCLE', 'SUPPLEMENTAL')),
    reference_cycle_id UUID REFERENCES payroll_cycles(id),
    status VARCHAR(20) NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'CALCULATING', 'REVIEW', 'APPROVED', 'DISBURSED', 'REJECTED')),
    
    -- Cycle Totals (Aggregates strictly for CLEARED records)
    total_gross NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_epf_employee NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_eps_employer NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_epf_employer NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_esi_employee NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_esi_employer NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_pt NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_tds NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_reimbursements NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_advance_deductions NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_net_payable NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_company_cost NUMERIC(14,2) NOT NULL DEFAULT 0,
    employee_count INTEGER NOT NULL DEFAULT 0,
    
    -- Banking & Compliance References
    bank_batch_reference TEXT,
    bank_export_url TEXT,
    ecr_export_url TEXT,
    
    -- Audit & Authorization
    created_by UUID REFERENCES users(id),
    approved_by UUID REFERENCES users(id),
    disbursed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Prevent accidental duplicate regular cycles for the same billing month
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_regular_cycle 
ON payroll_cycles (cycle_year, cycle_month) 
WHERE cycle_type = 'REGULAR';

CREATE INDEX IF NOT EXISTS idx_cycles_status ON payroll_cycles(status);

-- ------------------------------------------------------------------------------
-- 9. PAYROLL ENTRIES (Itemized Employee Payout Ledger & Snapshots)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id UUID NOT NULL REFERENCES payroll_cycles(id) ON DELETE CASCADE,
    employee_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    
    -- Exception Handling: Line-Item Hold Pattern
    status VARCHAR(20) NOT NULL DEFAULT 'CLEARED' CHECK (status IN ('CLEARED', 'ON_HOLD')),
    hold_reason TEXT,
    
    -- Calendar & Working Day Tracking
    total_calendar_days INTEGER NOT NULL DEFAULT 30,
    days_worked NUMERIC(4,1) NOT NULL DEFAULT 30,
    unpaid_leave_days NUMERIC(4,1) NOT NULL DEFAULT 0,
    unexcused_absent_days NUMERIC(4,1) NOT NULL DEFAULT 0,
    ncp_days NUMERIC(4,1) NOT NULL DEFAULT 0,     -- Non-Contributing Period for EPFO ECR
    
    -- Earned Salary Breakdown (Prorated)
    earned_basic NUMERIC(12,2) NOT NULL DEFAULT 0,
    earned_hra NUMERIC(12,2) NOT NULL DEFAULT 0,
    earned_special_allowance NUMERIC(12,2) NOT NULL DEFAULT 0,
    earned_da NUMERIC(12,2) NOT NULL DEFAULT 0,
    gross_earnings NUMERIC(12,2) NOT NULL DEFAULT 0,
    
    -- Statutory Wage Bases
    epf_wages NUMERIC(12,2) NOT NULL DEFAULT 0,
    eps_wages NUMERIC(12,2) NOT NULL DEFAULT 0,
    esi_wages NUMERIC(12,2) NOT NULL DEFAULT 0,
    
    -- Statutory Deductions & Employer Liabilities
    epf_employee_share NUMERIC(12,2) NOT NULL DEFAULT 0,  -- 12% EE
    eps_employer_share NUMERIC(12,2) NOT NULL DEFAULT 0,  -- 8.33% capped at ₹1,250
    epf_employer_share NUMERIC(12,2) NOT NULL DEFAULT 0,  -- 3.67% ER
    edli_employer_share NUMERIC(12,2) NOT NULL DEFAULT 0, -- 0.5%
    epf_admin_charges NUMERIC(12,2) NOT NULL DEFAULT 0,   -- 0.5%
    esi_employee_share NUMERIC(12,2) NOT NULL DEFAULT 0,  -- 0.75%
    esi_employer_share NUMERIC(12,2) NOT NULL DEFAULT 0,  -- 3.25%
    professional_tax NUMERIC(12,2) NOT NULL DEFAULT 0,
    tds_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    
    -- Payout Adjustments
    reimbursements_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    advance_recovery_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    net_payable NUMERIC(12,2) NOT NULL DEFAULT 0,
    gratuity_accrual_monthly NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_employer_cost NUMERIC(12,2) NOT NULL DEFAULT 0,
    
    -- Immutable Financial Snapshot (JSONB)
    earnings_breakdown JSONB NOT NULL DEFAULT '{}',
    deductions_breakdown JSONB NOT NULL DEFAULT '{}',
    
    -- Disbursement Status & Artifacts
    payslip_pdf_url TEXT,
    bank_utr_number TEXT,
    payout_status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK (payout_status IN ('PENDING', 'PAID', 'FAILED')),
    paid_at TIMESTAMPTZ,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(cycle_id, employee_id)
);

CREATE INDEX IF NOT EXISTS idx_entries_cycle ON payroll_entries(cycle_id);
CREATE INDEX IF NOT EXISTS idx_entries_emp ON payroll_entries(employee_id);
CREATE INDEX IF NOT EXISTS idx_entries_cycle_status ON payroll_entries(cycle_id, status);

-- ------------------------------------------------------------------------------
-- 10. PAYROLL CYCLE APPROVALS (Multi-Tier Audit Trail)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll_cycle_approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id UUID NOT NULL REFERENCES payroll_cycles(id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL CHECK (step_order >= 1 AND step_order <= 3),
    role_required VARCHAR(50) NOT NULL,           -- 'hr_admin', 'finance_manager', 'cfo'
    approver_id UUID REFERENCES users(id),
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
    comments TEXT,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approvals_cycle ON payroll_cycle_approvals(cycle_id);

-- ------------------------------------------------------------------------------
-- 11. STATUTORY SEED DATA (Professional Tax Slabs & Company Establishment)
-- ------------------------------------------------------------------------------

-- Seed Company Statutory Details (Aimploy Technologies India Pvt Ltd)
INSERT INTO company_statutory_details (
    company_name, tan_number, pan_number, epfo_establishment_code, 
    esic_establishment_code, pt_rc_number, pt_ec_number, registered_address
)
SELECT
    'Aimploy Technologies India Private Limited',
    'BLRA12345C',
    'AABCA1234F',
    'BGBNG0012345000',
    '50000123450000001',
    'KA-PTRC-987654',
    'KA-PTEC-123456',
    'Tower B, Prestige Tech Park, Marathahalli-Sarjapur Outer Ring Road, Bengaluru, Karnataka 560103'
WHERE NOT EXISTS (SELECT 1 FROM company_statutory_details);

-- Seed PT State Slabs (Karnataka, Maharashtra, Telangana, Delhi) idempotently
INSERT INTO pt_state_slabs (state_code, state_name, min_monthly_gross, max_monthly_gross, pt_amount, special_feb_amount)
SELECT v.state_code, v.state_name, v.min_monthly_gross, v.max_monthly_gross, v.pt_amount, v.special_feb_amount
FROM (VALUES
    ('KA', 'Karnataka', 0.00::numeric, 15000.00::numeric, 0.00::numeric, NULL::numeric),
    ('KA', 'Karnataka', 15000.01::numeric, NULL::numeric, 200.00::numeric, NULL::numeric),
    ('MH', 'Maharashtra', 0.00::numeric, 7500.00::numeric, 0.00::numeric, NULL::numeric),
    ('MH', 'Maharashtra', 7500.01::numeric, 10000.00::numeric, 175.00::numeric, NULL::numeric),
    ('MH', 'Maharashtra', 10000.01::numeric, NULL::numeric, 200.00::numeric, 300.00::numeric),
    ('TS', 'Telangana', 0.00::numeric, 15000.00::numeric, 0.00::numeric, NULL::numeric),
    ('TS', 'Telangana', 15000.01::numeric, 20000.00::numeric, 150.00::numeric, NULL::numeric),
    ('TS', 'Telangana', 20000.01::numeric, NULL::numeric, 200.00::numeric, NULL::numeric),
    ('DL', 'Delhi', 0.00::numeric, NULL::numeric, 0.00::numeric, NULL::numeric)
) AS v(state_code, state_name, min_monthly_gross, max_monthly_gross, pt_amount, special_feb_amount)
WHERE NOT EXISTS (
    SELECT 1 FROM pt_state_slabs p
    WHERE p.state_code = v.state_code
      AND p.min_monthly_gross = v.min_monthly_gross
);
