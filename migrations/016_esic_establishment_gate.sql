-- Migration 016: Add establishment-level ESIC applicability gate
-- ESI Act, 1948 Sections 1(4) and 1(5) apply to establishments with 10 or more workers.
-- When is_esic_applicable is FALSE, ESIC deductions are bypassed globally across all employees.

ALTER TABLE company_statutory_details 
ADD COLUMN IF NOT EXISTS is_esic_applicable BOOLEAN NOT NULL DEFAULT false;

-- Enable ESIC if the company already has a non-empty esic_establishment_code configured
UPDATE company_statutory_details 
SET is_esic_applicable = true 
WHERE esic_establishment_code IS NOT NULL AND trim(esic_establishment_code) != '';
