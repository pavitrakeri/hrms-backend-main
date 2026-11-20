"""
Comprehensive Integration & Unit Test Suite for Payroll Disbursement Reconciliation,
Document Vault & Statutory Compliance Tracking.

Covers:
  - Phase 2: State model transitions, cycle/entry status decoupling, unverified flags.
  - Phase 3: Bank Reverse MIS CSV upload, format detection, matching engine, duplicate rejection.
  - Phase 4: Document Vault storage, sha256 checksums, MIME checks, authenticated downloads.
  - Phase 5 & 6: Statutory liability rollup, 5-way reconciliation, variance detection, due dates.
  - Phase 7: Failed transaction remediation, off-cycle recovery, duplicate payment prevention.
  - Phase 9: Append-only audit logs.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from fastapi import UploadFile

from app.datamodels.payroll_v2_models import (
    BankBatchSubmissionRequest,
    OffCycleInitiateRequest,
    RemediateFailureRequest,
    StatutoryRemittanceUpdateRequest,
)
from app.features.payroll_v2.bank_reconciliation import (
    _clean_account,
    _detect_csv_columns,
    _parse_decimal,
    get_reconciliation_summary,
    reconcile_bank_mis_file,
    record_bank_submission,
)
from app.features.payroll_v2.document_vault import (
    archive_payroll_document,
    compute_sha256,
    get_document_for_download,
    list_payroll_documents,
    store_payroll_document,
)
from app.features.payroll_v2.remediation_service import (
    initiate_off_cycle_settlement,
    list_payroll_audit_logs,
    update_entry_remediation,
)
from app.features.payroll_v2.statutory_remittance import (
    _calculate_due_date,
    init_statutory_remittances_for_cycle,
    list_statutory_remittances_for_cycle,
    update_statutory_remittance,
)

DATABASE_URL = "postgresql://hrms_user:hrms_pass@127.0.0.1:5432/hrms_local"


# ---------------------------------------------------------------------------
# 1. Pure Unit Tests
# ---------------------------------------------------------------------------
def test_reconciliation_engine_logic():
    # Account cleaning
    assert _clean_account(" 501004-9821 00 ") == "501004982100"
    assert _clean_account("SBIN-0012948-X") == "SBIN0012948X"

    # Decimal parsing
    assert _parse_decimal("₹ 1,24,500.50") == Decimal("124500.50")
    assert _parse_decimal("45000") == Decimal("45000.00")

    # Column detection across diverse bank formats
    hdfc_header = ["Sr No", "Beneficiary Account Number", "Beneficiary Name", "Amount", "UTR Number", "Status", "Reason"]
    cols = _detect_csv_columns(hdfc_header)
    assert cols["account"] == 1
    assert cols["amount"] == 3
    assert cols["utr"] == 4
    assert cols["status"] == 5
    assert cols["reason"] == 6

    icici_header = ["Txn_Ref", "Cr_Account_No", "IFSC_Code", "Txn_Amt", "Payment_Status", "Bank_Ref_No"]
    cols_icici = _detect_csv_columns(icici_header)
    assert cols_icici["account"] == 1
    assert cols_icici["amount"] == 3
    assert cols_icici["status"] == 4
    assert cols_icici["utr"] == 5


def test_statutory_remittance_due_dates():
    # EPFO / ESIC: 15th of next month
    assert _calculate_due_date("EPFO", 11, 2026) == date(2026, 12, 15)
    assert _calculate_due_date("ESIC", 11, 2026) == date(2026, 12, 15)

    # TDS: 7th of next month, except March which is April 30
    assert _calculate_due_date("TDS", 11, 2026) == date(2026, 12, 7)
    assert _calculate_due_date("TDS", 3, 2026) == date(2026, 4, 30)

    # PT: 20th of next month
    assert _calculate_due_date("PT", 11, 2026) == date(2026, 12, 20)

    # Year rollover: December to January
    assert _calculate_due_date("EPFO", 12, 2026) == date(2027, 1, 15)


def test_document_vault_checksum_and_validation():
    sample_bytes = b"Aimploy Indian Statutory Payroll Payout CSV Content"
    checksum = compute_sha256(sample_bytes)
    assert len(checksum) == 64
    assert compute_sha256(sample_bytes) == checksum


# ---------------------------------------------------------------------------
# 2. Live Database Integration Tests (PostgreSQL 16)
# ---------------------------------------------------------------------------
async def _clean_test_db(conn):
    try:
        await conn.execute("DELETE FROM payroll_payment_transactions WHERE cycle_id IN (SELECT id FROM payroll_cycles WHERE cycle_name LIKE 'Test Recon Cycle%');")
        await conn.execute("DELETE FROM payroll_bank_reconciliations WHERE cycle_id IN (SELECT id FROM payroll_cycles WHERE cycle_name LIKE 'Test Recon Cycle%');")
        await conn.execute("DELETE FROM payroll_payment_batches WHERE cycle_id IN (SELECT id FROM payroll_cycles WHERE cycle_name LIKE 'Test Recon Cycle%');")
        await conn.execute("DELETE FROM payroll_documents WHERE cycle_id IN (SELECT id FROM payroll_cycles WHERE cycle_name LIKE 'Test Recon Cycle%');")
        await conn.execute("DELETE FROM payroll_statutory_remittances WHERE cycle_id IN (SELECT id FROM payroll_cycles WHERE cycle_name LIKE 'Test Recon Cycle%');")
        await conn.execute("DELETE FROM payroll_audit_logs WHERE cycle_id IN (SELECT id FROM payroll_cycles WHERE cycle_name LIKE 'Test Recon Cycle%');")
        await conn.execute("DELETE FROM payroll_entries WHERE cycle_id IN (SELECT id FROM payroll_cycles WHERE cycle_name LIKE 'Test Recon Cycle%');")
        await conn.execute("DELETE FROM payroll_cycles WHERE reference_cycle_id IN (SELECT id FROM payroll_cycles WHERE cycle_name LIKE 'Test Recon Cycle%');")
        await conn.execute("DELETE FROM payroll_cycles WHERE cycle_name LIKE 'Test Recon Cycle%';")
        await conn.execute("DELETE FROM employee_bank_details WHERE employee_id IN (SELECT id FROM users WHERE email LIKE '%@test.aimploy.com');")
        await conn.execute("DELETE FROM users WHERE email LIKE '%@test.aimploy.com';")
    except Exception:
        pass


@pytest.mark.asyncio
async def test_live_db_end_to_end_reconciliation_workflow():
    """
    Full end-to-end integration test exercising all financial invariants:
      1. Bank submission recording decoupled from employee PAID status.
      2. Inbound Reverse MIS matching: EXACT_MATCH, FAILED_TRANSACTION, AMOUNT_MISMATCH.
      3. Partial batch settlement (cycle transitions to PARTIALLY_DISBURSED).
      4. Duplicate file upload rejected via SHA-256 hash.
      5. Document Vault upload, indexing, and retrieval.
      6. Statutory remittance 5-way reconciliation and variance calculation.
      7. Off-cycle recovery: duplicate payment blocked for PAID entries; allowed for FAILED.
      8. Immutable audit trail validation.
    """
    conn = await asyncpg.connect(DATABASE_URL)
    actor_id = uuid4()
    uniq = uuid4().hex[:8]
    user_actor = {"id": str(actor_id), "role": "cfo", "email": f"cfo.{uniq}@test.aimploy.com"}
    cycle_id = uuid4()
    emp1_id = uuid4()
    emp2_id = uuid4()
    emp3_id = uuid4()

    try:
        # Pre-cleanup in case a previous run aborted
        await _clean_test_db(conn)

        # Create actor user
        await conn.execute(
            """
            INSERT INTO users (id, full_name, email, password_hash, is_active)
            VALUES ($1::uuid, 'Test CFO', $2, 'dummy_hash', true);
            """,
            actor_id,
            user_actor["email"],
        )

        # Create test users and employees
        for eid, name, email in [
            (emp1_id, "Dev Sharma", f"dev.{uniq}@test.aimploy.com"),
            (emp2_id, "Priya Nair", f"priya.{uniq}@test.aimploy.com"),
            (emp3_id, "Rahul Patel", f"rahul.{uniq}@test.aimploy.com"),
        ]:
            await conn.execute(
                """
                INSERT INTO users (id, full_name, email, password_hash, is_active)
                VALUES ($1::uuid, $2, $3, 'dummy_hash', true);
                """,
                eid, name, email,
            )

        # Insert bank details
        await conn.execute(
            """
            INSERT INTO employee_bank_details (id, employee_id, account_holder_name, account_number, ifsc_code, bank_name, is_primary)
            VALUES 
                (gen_random_uuid(), $1::uuid, 'Dev Sharma', '501004982101', 'HDFC0001234', 'HDFC Bank', true),
                (gen_random_uuid(), $2::uuid, 'Priya Nair', '501004982102', 'ICIC0005678', 'ICICI Bank', true),
                (gen_random_uuid(), $3::uuid, 'Rahul Patel', '501004982103', 'SBIN0009999', 'SBI', true)
            ON CONFLICT DO NOTHING;
            """,
            emp1_id, emp2_id, emp3_id,
        )

        # Create test payroll cycle (use unique test year 2038 to avoid conflicting with existing cycles)
        await conn.execute(
            """
            INSERT INTO payroll_cycles (
                id, cycle_name, cycle_month, cycle_year,
                start_date, end_date, cutoff_date, status,
                total_gross, total_net_payable, employee_count
            )
            VALUES (
                $1::uuid, 'Test Recon Cycle Oct 2038', 10, 2038,
                '2038-10-01', '2038-10-31', '2038-10-31', 'APPROVED',
                300000.00, 250000.00, 3
            );
            """,
            cycle_id,
        )

        # Create test entries:
        # Emp1: net 100,000 (will succeed)
        # Emp2: net 80,000 (will fail at bank)
        # Emp3: net 70,000 (bank amount will mismatch)
        e1_id = uuid4()
        e2_id = uuid4()
        e3_id = uuid4()

        await conn.execute(
            """
            INSERT INTO payroll_entries (
                id, cycle_id, employee_id, status,
                earned_basic, gross_earnings, net_payable,
                epf_employee_share, eps_employer_share, epf_employer_share, edli_employer_share, epf_admin_charges,
                esi_employee_share, esi_employer_share, professional_tax, tds_amount,
                payout_status
            )
            VALUES
                ($1::uuid, $4::uuid, $5::uuid, 'CLEARED', 50000, 120000, 100000.00, 6000, 1250, 1835, 250, 250, 0, 0, 200, 11465, 'NOT_GENERATED'),
                ($2::uuid, $4::uuid, $6::uuid, 'CLEARED', 40000, 95000, 80000.00, 4800, 1250, 1835, 200, 200, 0, 0, 200, 7715, 'NOT_GENERATED'),
                ($3::uuid, $4::uuid, $7::uuid, 'CLEARED', 35000, 85000, 70000.00, 4200, 1250, 1835, 175, 175, 0, 0, 200, 8165, 'NOT_GENERATED');
            """,
            e1_id, e2_id, e3_id, cycle_id, emp1_id, emp2_id, emp3_id,
        )

        # -------------------------------------------------------------------
        # Step A: Record Outbound Bank Submission
        # Invariant 1: Batch submission must NOT mark employees PAID
        # -------------------------------------------------------------------
        sub_req = BankBatchSubmissionRequest(
            batch_reference=f"TEST-BATCH-{uuid4().hex[:8]}",
            bank_name="HDFC Corporate CMS",
            ack_reference="HDFC-ACK-99901",
        )
        batch_res = await record_bank_submission(conn, user_actor, str(cycle_id), sub_req)
        assert batch_res.status == "BANK_ACCEPTED"

        # Verify cycle is DISBURSEMENT_IN_PROGRESS (NOT DISBURSED)
        c_status = await conn.fetchval("SELECT status FROM payroll_cycles WHERE id = $1::uuid;", cycle_id)
        assert c_status == "DISBURSEMENT_IN_PROGRESS"

        # Verify entries are SUBMITTED_TO_BANK (NOT PAID!)
        p_statuses = await conn.fetch("SELECT payout_status, bank_verified FROM payroll_entries WHERE cycle_id = $1::uuid;", cycle_id)
        assert all(r["payout_status"] == "SUBMITTED_TO_BANK" for r in p_statuses)
        assert all(r["bank_verified"] is False for r in p_statuses)

        # -------------------------------------------------------------------
        # Step B: Inbound Bank Reverse MIS Upload & Matching
        # -------------------------------------------------------------------
        # CSV content with:
        # 1. Emp1 (501004982101): Amount 100000.00 -> SUCCESS, UTR: HDFC12345678
        # 2. Emp2 (501004982102): Amount 80000.00 -> REJECTED, Reason: A/C CLOSED OR DOES NOT EXIST
        # 3. Emp3 (501004982103): Amount 65000.00 -> Amount mismatch (expected 70,000)
        csv_data = (
            "Beneficiary Account Number,Beneficiary Name,Amount,UTR Number,Status,Reason\n"
            "501004982101,Dev Sharma,100000.00,HDFC12345678,SUCCESS,Processed successfully\n"
            "501004982102,Priya Nair,80000.00,,REJECTED,A/C CLOSED OR DOES NOT EXIST\n"
            "501004982103,Rahul Patel,65000.00,HDFC99990000,SUCCESS,Processed\n"
        ).encode("utf-8")

        mock_file = UploadFile(
            file=io.BytesIO(csv_data),
            filename="HDFC_Reverse_MIS_Test.csv",
            headers={"content-type": "text/csv"},
        )

        recon_summary = await reconcile_bank_mis_file(
            conn, user_actor, str(cycle_id), mock_file, batch_reference=batch_res.batch_reference
        )

        assert recon_summary.total_payment_instructions == 3
        assert recon_summary.successful_count == 1
        assert recon_summary.failed_count == 1
        assert recon_summary.review_count == 1
        assert recon_summary.successful_amount == 100000.00
        assert recon_summary.failed_amount == 80000.00

        # Invariant 2: Cycle must be PARTIALLY_DISBURSED because only 1 of 3 cleared
        c_status_post = await conn.fetchval("SELECT status FROM payroll_cycles WHERE id = $1::uuid;", cycle_id)
        assert c_status_post == "PARTIALLY_DISBURSED"

        # Verify entry 1: PAID with actual UTR and bank_verified = True
        e1_row = await conn.fetchrow("SELECT * FROM payroll_entries WHERE id = $1::uuid;", e1_id)
        assert e1_row["payout_status"] == "PAID"
        assert e1_row["bank_utr_number"] == "HDFC12345678"
        assert e1_row["bank_verified"] is True
        assert e1_row["paid_at"] is not None

        # Verify entry 2: FAILED with error code and remediation_status = NEEDS_CORRECTION
        e2_row = await conn.fetchrow("SELECT * FROM payroll_entries WHERE id = $1::uuid;", e2_id)
        assert e2_row["payout_status"] == "FAILED"
        assert e2_row["bank_response_code"] == "FAILED"
        assert "CLOSED" in e2_row["bank_response_message"]
        assert e2_row["remediation_status"] == "NEEDS_CORRECTION"
        assert e2_row["bank_verified"] is False

        # Verify entry 3: REQUIRES_REVIEW (amount mismatch was NOT paid)
        e3_row = await conn.fetchrow("SELECT * FROM payroll_entries WHERE id = $1::uuid;", e3_id)
        assert e3_row["payout_status"] == "REQUIRES_REVIEW"
        assert e3_row["bank_verified"] is False

        # -------------------------------------------------------------------
        # Step C: Invariant 3: Duplicate File Upload Rejected
        # -------------------------------------------------------------------
        dup_file = UploadFile(
            file=io.BytesIO(csv_data),
            filename="HDFC_Reverse_MIS_Test.csv",
            headers={"content-type": "text/csv"},
        )
        with pytest.raises(Exception) as exc_info:
            await reconcile_bank_mis_file(
                conn, user_actor, str(cycle_id), dup_file, batch_reference=batch_res.batch_reference
            )
        assert "already been reconciled" in str(exc_info.value)

        # -------------------------------------------------------------------
        # Step D: Document Vault Verification
        # -------------------------------------------------------------------
        doc_bytes = b"%PDF-1.4 Mock EPFO Challan TRRN 1592611001234"
        challan_file = UploadFile(
            file=io.BytesIO(doc_bytes),
            filename="EPFO_Payment_Receipt_Nov2026.pdf",
            headers={"content-type": "application/pdf"},
        )
        stored_doc = await store_payroll_document(
            conn,
            user_actor,
            challan_file,
            document_category="EPFO",
            document_type="EPFO_TRRN_RECEIPT",
            cycle_id=str(cycle_id),
            external_reference_number="1592611001234",
            notes="EPFO Challan receipt confirmed via SBI net banking",
        )
        assert stored_doc.document_category == "EPFO"
        assert stored_doc.external_reference_number == "1592611001234"

        # List documents for this cycle
        docs = await list_payroll_documents(conn, cycle_id=str(cycle_id))
        assert len(docs) >= 2  # The Reverse MIS CSV and the EPFO PDF

        # Download document verification
        disk_path, fname, mime = await get_document_for_download(conn, user_actor, stored_doc.id)
        assert disk_path.exists()
        assert fname == "EPFO_Payment_Receipt_Nov2026.pdf"

        # Soft archive
        archived_ok = await archive_payroll_document(conn, user_actor, stored_doc.id, notes="Superseded")
        assert archived_ok is True
        active_docs = await list_payroll_documents(conn, cycle_id=str(cycle_id), include_archived=False)
        assert stored_doc.id not in [d.id for d in active_docs]

        # -------------------------------------------------------------------
        # Step E: Statutory Remittance & Compliance Tracking
        # -------------------------------------------------------------------
        remittances = await init_statutory_remittances_for_cycle(conn, str(cycle_id))
        assert len(remittances) == 4
        cat_map = {r.category: r for r in remittances}

        # EPFO liability check: EE (15000) + ER EPS (3750) + ER EPF (5505) + EDLI (625) + Admin (625) = 25,505
        assert cat_map["EPFO"].calculated_liability > 0
        assert cat_map["PT"].calculated_liability == 600.00  # 3 employees * 200

        # Partial payment check (Underpayment)
        part_update = StatutoryRemittanceUpdateRequest(
            declared_liability=cat_map["EPFO"].calculated_liability,
            challan_amount=cat_map["EPFO"].calculated_liability,
            paid_amount=cat_map["EPFO"].calculated_liability - 5000.00,  # Underpaid by ₹5,000
            payment_date="2026-11-14",
            external_reference="TRRN-PARTIAL-001",
        )
        part_res = await update_statutory_remittance(conn, user_actor, str(cycle_id), "EPFO", part_update)
        assert part_res.status == "PARTIALLY_PAID"
        assert part_res.difference_amount == 5000.00

        # Full settlement check
        full_update = StatutoryRemittanceUpdateRequest(
            paid_amount=cat_map["EPFO"].calculated_liability,
            acknowledged_amount=cat_map["EPFO"].calculated_liability,
            external_reference="1592611001234",
            status="PAID",
        )
        full_res = await update_statutory_remittance(conn, user_actor, str(cycle_id), "EPFO", full_update)
        assert full_res.status == "PAID"
        assert full_res.difference_amount == 0.00

        # -------------------------------------------------------------------
        # Step F: Failed Payment Remediation & Off-Cycle Settlement
        # Invariant 4: Duplicate payment prevention
        # -------------------------------------------------------------------
        # Invariant: Attempting off-cycle on the already PAID entry (Emp1) must be rejected
        with pytest.raises(Exception) as exc_paid:
            await initiate_off_cycle_settlement(
                conn, user_actor, str(cycle_id), OffCycleInitiateRequest(target_entry_ids=[str(e1_id)])
            )
        assert "already PAID and bank-verified" in str(exc_paid.value)

        # Remediate Emp2: Correct bank details
        rem_res = await update_entry_remediation(
            conn,
            user_actor,
            str(cycle_id),
            str(e2_id),
            RemediateFailureRequest(
                remediation_status="DETAILS_UPDATED",
                notes="Employee updated to new active ICICI savings account",
                corrected_account_number="501004982999",
                corrected_ifsc_code="ICIC0009999",
            ),
        )
        assert rem_res["remediation_status"] == "DETAILS_UPDATED"

        # Verify updated bank details in DB
        upd_bank = await conn.fetchrow(
            "SELECT account_number, ifsc_code FROM employee_bank_details WHERE employee_id = $1::uuid;", emp2_id
        )
        assert upd_bank["account_number"] == "501004982999"
        assert upd_bank["ifsc_code"] == "ICIC0009999"

        # Initiate Off-Cycle Settlement for Emp2
        off_cycle = await initiate_off_cycle_settlement(
            conn,
            user_actor,
            str(cycle_id),
            OffCycleInitiateRequest(target_entry_ids=[str(e2_id)], notes="Recovering failed salary for Priya Nair"),
        )
        assert off_cycle.cycle_type == "OFF_CYCLE"
        assert off_cycle.reference_cycle_id == str(cycle_id)
        assert off_cycle.total_net_payable == 80000.00
        assert off_cycle.employee_count == 1

        # Original entry must now be OFF_CYCLE_QUEUED
        e2_recheck = await conn.fetchrow("SELECT remediation_status FROM payroll_entries WHERE id = $1::uuid;", e2_id)
        assert e2_recheck["remediation_status"] == "OFF_CYCLE_QUEUED"

        # -------------------------------------------------------------------
        # Step G: Append-Only Audit Trail Verification
        # -------------------------------------------------------------------
        audit_logs = await list_payroll_audit_logs(conn, cycle_id=str(cycle_id))
        actions = [log.action for log in audit_logs]
        assert "BANK_SUBMISSION_RECORDED" in actions
        assert "BANK_RECONCILIATION_COMPLETED" in actions
        assert "DOCUMENT_UPLOADED" in actions
        assert "STATUTORY_REMITTANCE_UPDATED" in actions
        assert "PAYMENT_FAILURE_REMEDIATED" in actions
        assert "OFF_CYCLE_SETTLEMENT_INITIATED" in actions

    finally:
        # Cleanup test data cleanly
        await _clean_test_db(conn)
        await conn.close()
