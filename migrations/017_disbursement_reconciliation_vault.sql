-- Migration 017: End-to-End Payroll Disbursement Reconciliation, Statutory Compliance Tracking & Document Vault
-- Supports:
--   1. Proper bank payment lifecycle states (NOT_GENERATED, GENERATED, SUBMITTED_TO_BANK, BANK_ACCEPTED, PROCESSING, PAID, FAILED, REVERSED, REQUIRES_REVIEW)
--   2. Separation of cycle status (DISBURSEMENT_IN_PROGRESS, PARTIALLY_DISBURSED, DISBURSED, LOCKED)
--   3. Secure Payroll Document Vault (metadata, sha256, private storage references)
--   4. Bank payment batches, reconciliation imports (Reverse MIS), and transaction matching
--   5. Comprehensive statutory liability & remittance tracking (EPFO, ESIC, PT, TDS)
--   6. Append-only financial audit logging

-- 1. Extend payroll_cycles status check constraint
ALTER TABLE payroll_cycles ALTER COLUMN status TYPE VARCHAR(40);
ALTER TABLE payroll_cycles DROP CONSTRAINT IF EXISTS payroll_cycles_status_check;
ALTER TABLE payroll_cycles ADD CONSTRAINT payroll_cycles_status_check 
CHECK (status IN ('DRAFT', 'CALCULATING', 'REVIEW', 'APPROVED', 'DISBURSEMENT_IN_PROGRESS', 'PARTIALLY_DISBURSED', 'DISBURSED', 'LOCKED', 'REJECTED'));

-- 2. Extend payroll_entries payout_status check constraint and add reconciliation/audit fields
ALTER TABLE payroll_entries ALTER COLUMN payout_status TYPE VARCHAR(40);
ALTER TABLE payroll_entries DROP CONSTRAINT IF EXISTS payroll_entries_payout_status_check;
ALTER TABLE payroll_entries ADD CONSTRAINT payroll_entries_payout_status_check 
CHECK (payout_status IN ('NOT_GENERATED', 'GENERATED', 'SUBMITTED_TO_BANK', 'BANK_ACCEPTED', 'PROCESSING', 'PAID', 'FAILED', 'REVERSED', 'REQUIRES_REVIEW', 'PENDING'));

ALTER TABLE payroll_entries 
ADD COLUMN IF NOT EXISTS bank_verified BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN IF NOT EXISTS bank_response_code TEXT,
ADD COLUMN IF NOT EXISTS bank_response_message TEXT,
ADD COLUMN IF NOT EXISTS bank_transaction_date TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS reconciled_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS reconciled_by UUID REFERENCES users(id),
ADD COLUMN IF NOT EXISTS reconciliation_source_id UUID,
ADD COLUMN IF NOT EXISTS remediation_notes TEXT,
ADD COLUMN IF NOT EXISTS remediation_status VARCHAR(30) NOT NULL DEFAULT 'NONE' CHECK (remediation_status IN ('NONE', 'NEEDS_CORRECTION', 'DETAILS_UPDATED', 'OFF_CYCLE_QUEUED', 'RESOLVED'));

-- Mark legacy entries that were previously set to PAID as unverified historical entries
UPDATE payroll_entries 
SET bank_verified = false 
WHERE payout_status = 'PAID' AND (bank_verified IS NULL OR bank_verified = false);

-- 3. Payroll Document Vault
CREATE TABLE IF NOT EXISTS payroll_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID,
    cycle_id UUID REFERENCES payroll_cycles(id) ON DELETE SET NULL,
    employee_id UUID REFERENCES users(id) ON DELETE SET NULL,
    document_category VARCHAR(50) NOT NULL CHECK (document_category IN ('BANKING', 'EPFO', 'ESIC', 'PROFESSIONAL_TAX', 'TDS', 'GENERAL_PAYROLL')),
    document_type VARCHAR(60) NOT NULL,
    financial_year VARCHAR(10),
    wage_month INTEGER,
    wage_year INTEGER,
    original_filename TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    mime_type TEXT NOT NULL,
    sha256_checksum TEXT NOT NULL,
    external_reference_number TEXT,
    notes TEXT,
    is_archived BOOLEAN NOT NULL DEFAULT false,
    uploaded_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payroll_docs_cycle ON payroll_documents(cycle_id);
CREATE INDEX IF NOT EXISTS idx_payroll_docs_category ON payroll_documents(document_category);
CREATE INDEX IF NOT EXISTS idx_payroll_docs_checksum ON payroll_documents(sha256_checksum);

-- 4. Corporate Bank Payment Batches
CREATE TABLE IF NOT EXISTS payroll_payment_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id UUID NOT NULL REFERENCES payroll_cycles(id) ON DELETE RESTRICT,
    batch_reference TEXT NOT NULL UNIQUE,
    bank_name TEXT NOT NULL,
    payment_mode VARCHAR(20) NOT NULL DEFAULT 'NEFT_RTGS',
    total_beneficiaries INTEGER NOT NULL DEFAULT 0,
    total_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    status VARCHAR(30) NOT NULL DEFAULT 'GENERATED' CHECK (status IN ('GENERATED', 'SUBMITTED_TO_BANK', 'BANK_ACCEPTED', 'PROCESSING', 'RECONCILED', 'PARTIALLY_SETTLED', 'FAILED')),
    export_filename TEXT,
    export_storage_path TEXT,
    file_checksum_sha256 TEXT,
    submission_timestamp TIMESTAMPTZ,
    ack_reference TEXT,
    ack_timestamp TIMESTAMPTZ,
    ack_document_id UUID REFERENCES payroll_documents(id) ON DELETE SET NULL,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payment_batches_cycle ON payroll_payment_batches(cycle_id);
CREATE INDEX IF NOT EXISTS idx_payment_batches_ref ON payroll_payment_batches(batch_reference);

-- 5. Bank Reconciliation Uploads (Reverse MIS)
CREATE TABLE IF NOT EXISTS payroll_bank_reconciliations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id UUID NOT NULL REFERENCES payroll_cycles(id) ON DELETE RESTRICT,
    batch_id UUID REFERENCES payroll_payment_batches(id) ON DELETE SET NULL,
    document_id UUID REFERENCES payroll_documents(id) ON DELETE SET NULL,
    bank_format VARCHAR(50) NOT NULL DEFAULT 'STANDARD_REVERSE_MIS',
    file_checksum_sha256 TEXT NOT NULL,
    total_rows INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    reversal_count INTEGER NOT NULL DEFAULT 0,
    total_matched_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_failed_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    status VARCHAR(30) NOT NULL DEFAULT 'COMPLETED' CHECK (status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')),
    uploaded_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reconciliations_cycle ON payroll_bank_reconciliations(cycle_id);
CREATE INDEX IF NOT EXISTS idx_reconciliations_checksum ON payroll_bank_reconciliations(file_checksum_sha256);

-- 6. Bank Transaction Line Items from Reverse MIS
CREATE TABLE IF NOT EXISTS payroll_payment_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_id UUID NOT NULL REFERENCES payroll_bank_reconciliations(id) ON DELETE CASCADE,
    cycle_id UUID NOT NULL REFERENCES payroll_cycles(id),
    payroll_entry_id UUID REFERENCES payroll_entries(id) ON DELETE SET NULL,
    employee_id UUID REFERENCES users(id) ON DELETE SET NULL,
    raw_account_number TEXT,
    account_number_mask TEXT,
    ifsc_code TEXT,
    amount NUMERIC(14,2) NOT NULL,
    bank_utr TEXT,
    bank_status VARCHAR(30) NOT NULL,
    bank_response_code TEXT,
    bank_response_message TEXT,
    transaction_timestamp TIMESTAMPTZ,
    match_outcome VARCHAR(30) NOT NULL CHECK (match_outcome IN ('EXACT_MATCH', 'PARTIAL_MATCH', 'AMOUNT_MISMATCH', 'ACCOUNT_MISMATCH', 'DUPLICATE_BANK_RECORD', 'UNMATCHED', 'FAILED_TRANSACTION', 'REVERSED_TRANSACTION', 'REQUIRES_REVIEW')),
    is_settled BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pay_trans_reconciliation ON payroll_payment_transactions(reconciliation_id);
CREATE INDEX IF NOT EXISTS idx_pay_trans_entry ON payroll_payment_transactions(payroll_entry_id);
CREATE INDEX IF NOT EXISTS idx_pay_trans_utr ON payroll_payment_transactions(bank_utr);

-- 7. Statutory Liability & Remittance Tracking (EPFO, ESIC, PT, TDS)
CREATE TABLE IF NOT EXISTS payroll_statutory_remittances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id UUID NOT NULL REFERENCES payroll_cycles(id) ON DELETE CASCADE,
    category VARCHAR(20) NOT NULL CHECK (category IN ('EPFO', 'ESIC', 'PT', 'TDS')),
    financial_year VARCHAR(10) NOT NULL,
    wage_month INTEGER NOT NULL,
    wage_year INTEGER NOT NULL,
    calculated_liability NUMERIC(14,2) NOT NULL DEFAULT 0,
    declared_liability NUMERIC(14,2) NOT NULL DEFAULT 0,
    challan_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    paid_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    acknowledged_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    status VARCHAR(30) NOT NULL DEFAULT 'NOT_GENERATED' CHECK (status IN ('NOT_GENERATED', 'GENERATED', 'SUBMITTED', 'ACKNOWLEDGED', 'CHALLAN_GENERATED', 'PAYMENT_PENDING', 'PARTIALLY_PAID', 'PAID', 'RECONCILED', 'REQUIRES_REVIEW', 'FAILED')),
    due_date DATE NOT NULL,
    payment_date DATE,
    filing_date DATE,
    external_reference TEXT,
    token_number TEXT,
    challan_document_id UUID REFERENCES payroll_documents(id) ON DELETE SET NULL,
    receipt_document_id UUID REFERENCES payroll_documents(id) ON DELETE SET NULL,
    notes TEXT,
    reconciled_by UUID REFERENCES users(id),
    reconciled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cycle_id, category)
);

CREATE INDEX IF NOT EXISTS idx_statutory_remittances_cycle ON payroll_statutory_remittances(cycle_id);
CREATE INDEX IF NOT EXISTS idx_statutory_remittances_cat ON payroll_statutory_remittances(category);

-- 8. Append-Only Financial Audit Trail
CREATE TABLE IF NOT EXISTS payroll_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id UUID REFERENCES payroll_cycles(id) ON DELETE SET NULL,
    entry_id UUID REFERENCES payroll_entries(id) ON DELETE SET NULL,
    actor_id UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_role TEXT,
    action VARCHAR(60) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,
    entity_id TEXT NOT NULL,
    previous_state JSONB,
    new_state JSONB,
    comments TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payroll_audit_cycle ON payroll_audit_logs(cycle_id);
CREATE INDEX IF NOT EXISTS idx_payroll_audit_entry ON payroll_audit_logs(entry_id);
CREATE INDEX IF NOT EXISTS idx_payroll_audit_action ON payroll_audit_logs(action);
