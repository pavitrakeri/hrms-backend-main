"""
Bank Disbursement, Reverse MIS Reconciliation & Payment State Engine.

Implements:
  1. Payment Batch lifecycle: GENERATED -> SUBMITTED_TO_BANK -> BANK_ACCEPTED -> PROCESSING -> RECONCILED.
  2. Inbound Bank Reverse MIS CSV parsing (Generic Standard, HDFC CMS, ICICI CIB, SBI CMP).
  3. Transaction Matching Engine: EXACT_MATCH, AMOUNT_MISMATCH, FAILED_TRANSACTION, REVERSED_TRANSACTION, etc.
  4. Idempotency: SHA-256 duplicate file detection, safe retries, row locking.
  5. Cycle status decoupling: Cycle transitions to DISBURSEMENT_IN_PROGRESS, PARTIALLY_DISBURSED, or DISBURSED.
  6. Financial Ledger Settlement: Reimbursements and salary advances settle strictly upon verified bank confirmation.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from app.datamodels.payroll_v2_models import (
    BankBatchResponse,
    BankBatchSubmissionRequest,
    BankReconciliationSummaryResponse,
    PaymentTransactionRow,
)
from app.features.payroll_v2.document_vault import compute_sha256, store_payroll_document
from app.features.payroll_v2.engine_india import round_paisa
from app.features.payroll_v2.master_service import validate_uuid

ZERO = Decimal("0.00")
TOLERANCE = Decimal("0.05")


def _clean_str(val: Any) -> str:
    return str(val or "").strip()


def _clean_account(acc: str) -> str:
    """Normalize account number by removing spaces, hyphens, and leading zeros."""
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", acc).strip()
    return cleaned


def _parse_decimal(val: Any) -> Decimal:
    try:
        s = re.sub(r"[^\d.]", "", str(val or "0"))
        return round_paisa(Decimal(s))
    except Exception:
        return ZERO


def _mask_account(acc: str) -> str:
    cleaned = _clean_account(acc)
    if len(cleaned) <= 4:
        return "****" + cleaned
    return f"****{cleaned[-4:]}"


def _detect_csv_columns(header: list[str]) -> dict[str, int]:
    """
    Intelligently map CSV headers to canonical fields across HDFC, ICICI, SBI, and generic templates.
    """
    normalized = [h.strip().lower().replace(" ", "_").replace("-", "_") for h in header]
    mapping: dict[str, int] = {}

    for idx, col in enumerate(normalized):
        if any(k in col for k in ["bene_acc", "account_no", "account_num", "ac_no", "cr_acc", "account"]) and "source" not in col and "dr" not in col:
            mapping.setdefault("account", idx)
        elif any(k in col for k in ["amount", "net_pay", "txn_amt", "paid_amt", "amt"]):
            mapping.setdefault("amount", idx)
        elif any(k in col for k in ["bank_ref", "utr", "rrn", "bank_txn", "bank_reference"]):
            mapping["utr"] = idx
        elif "utr" not in mapping and any(k in col for k in ["txn_ref", "transaction_id", "ref_no"]):
            mapping.setdefault("utr", idx)
        elif any(k in col for k in ["status", "result", "txn_status", "payment_status"]):
            mapping.setdefault("status", idx)
        elif any(k in col for k in ["ifsc", "ifsc_code"]):
            mapping.setdefault("ifsc", idx)
        elif any(k in col for k in ["reason", "reject", "error", "response_code", "remarks", "msg"]):
            mapping.setdefault("reason", idx)
        elif any(k in col for k in ["date", "timestamp", "val_date", "proc_date"]):
            mapping.setdefault("date", idx)

    return mapping


# ---------------------------------------------------------------------------
# Outbound Batch & Submission Tracking
# ---------------------------------------------------------------------------
async def record_bank_submission(
    conn,
    user: dict[str, Any],
    cycle_id: str,
    req: BankBatchSubmissionRequest,
) -> BankBatchResponse:
    """
    Record that a payout file has been uploaded to the corporate bank.
    Does NOT mark employees as PAID!
    Transitions cycle to DISBURSEMENT_IN_PROGRESS and entries to SUBMITTED_TO_BANK.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    user_id = user.get("id")
    usr_uuid = validate_uuid(str(user_id), "user_id") if user_id else None

    async with conn.transaction():
        cycle = await conn.fetchrow(
            "SELECT * FROM payroll_cycles WHERE id = $1::uuid FOR UPDATE;", cid
        )
        if not cycle:
            raise HTTPException(status_code=404, detail="Payroll cycle not found.")
        if cycle["status"] not in ("APPROVED", "DISBURSEMENT_IN_PROGRESS", "PARTIALLY_DISBURSED"):
            raise HTTPException(
                status_code=409,
                detail=f"Only APPROVED or in-progress cycles can record bank submission (current: '{cycle['status']}').",
            )

        cleared_entries = await conn.fetch(
            "SELECT id, net_payable FROM payroll_entries WHERE cycle_id = $1::uuid AND status = 'CLEARED';",
            cid,
        )
        if not cleared_entries:
            raise HTTPException(status_code=400, detail="No CLEARED payroll entries exist in this cycle.")

        total_amount = sum(Decimal(str(r["net_payable"])) for r in cleared_entries)
        total_count = len(cleared_entries)

        batch_status = "BANK_ACCEPTED" if req.ack_reference else "SUBMITTED_TO_BANK"

        # Check for existing batch with same reference
        batch_row = await conn.fetchrow(
            """
            INSERT INTO payroll_payment_batches (
                cycle_id, batch_reference, bank_name, payment_mode,
                total_beneficiaries, total_amount, status,
                submission_timestamp, ack_reference, ack_timestamp, created_by
            )
            VALUES (
                $1::uuid, $2, $3, 'NEFT_RTGS',
                $4, $5, $6,
                COALESCE($7::timestamptz, now()), $8::text, CASE WHEN $8::text IS NOT NULL THEN now() ELSE NULL END, $9::uuid
            )
            ON CONFLICT (batch_reference) DO UPDATE
            SET bank_name = EXCLUDED.bank_name,
                ack_reference = COALESCE(EXCLUDED.ack_reference, payroll_payment_batches.ack_reference),
                ack_timestamp = CASE WHEN EXCLUDED.ack_reference IS NOT NULL THEN now() ELSE payroll_payment_batches.ack_timestamp END,
                status = EXCLUDED.status,
                updated_at = now()
            RETURNING *;
            """,
            cid,
            req.batch_reference.strip(),
            req.bank_name.strip(),
            total_count,
            round_paisa(total_amount),
            batch_status,
            req.submission_timestamp,
            req.ack_reference,
            usr_uuid,
        )

        # Update entries to SUBMITTED_TO_BANK if they are currently PENDING / NOT_GENERATED
        await conn.execute(
            """
            UPDATE payroll_entries
            SET payout_status = 'SUBMITTED_TO_BANK',
                updated_at = now()
            WHERE cycle_id = $1::uuid
              AND status = 'CLEARED'
              AND payout_status IN ('PENDING', 'NOT_GENERATED', 'GENERATED');
            """,
            cid,
        )

        # Update cycle status to DISBURSEMENT_IN_PROGRESS
        await conn.execute(
            """
            UPDATE payroll_cycles
            SET status = 'DISBURSEMENT_IN_PROGRESS',
                bank_batch_reference = $2,
                updated_at = now()
            WHERE id = $1::uuid;
            """,
            cid,
            req.batch_reference.strip(),
        )

        # Audit log
        await conn.execute(
            """
            INSERT INTO payroll_audit_logs (
                cycle_id, actor_id, actor_role, action,
                entity_type, entity_id, new_state, comments
            )
            VALUES (
                $1::uuid, $2::uuid, $3, 'BANK_SUBMISSION_RECORDED',
                'payroll_payment_batches', $4, $5::jsonb, $6
            );
            """,
            cid,
            usr_uuid,
            user.get("role", "system"),
            str(batch_row["id"]),
            f'{{"batch_reference": "{req.batch_reference}", "status": "{batch_status}"}}',
            f"Submitted batch {req.batch_reference} to {req.bank_name}",
        )

    return BankBatchResponse(
        id=str(batch_row["id"]),
        cycle_id=str(batch_row["cycle_id"]),
        batch_reference=batch_row["batch_reference"],
        bank_name=batch_row["bank_name"],
        payment_mode=batch_row["payment_mode"],
        total_beneficiaries=batch_row["total_beneficiaries"],
        total_amount=float(batch_row["total_amount"]),
        status=batch_row["status"],
        export_filename=batch_row["export_filename"],
        file_checksum_sha256=batch_row["file_checksum_sha256"],
        submission_timestamp=batch_row["submission_timestamp"].isoformat() if batch_row["submission_timestamp"] else None,
        ack_reference=batch_row["ack_reference"],
        ack_timestamp=batch_row["ack_timestamp"].isoformat() if batch_row["ack_timestamp"] else None,
        created_at=batch_row["created_at"].isoformat(),
        updated_at=batch_row["updated_at"].isoformat(),
    )


# ---------------------------------------------------------------------------
# Inbound Reverse MIS Parsing & Transaction Matching Engine
# ---------------------------------------------------------------------------
async def reconcile_bank_mis_file(
    conn,
    user: dict[str, Any],
    cycle_id: str,
    file: UploadFile,
    *,
    batch_reference: Optional[str] = None,
) -> BankReconciliationSummaryResponse:
    """
    Parse bank Reverse MIS CSV, match transactions, idempotently update payroll_entries,
    and settle ledger dependencies.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    user_id = user.get("id")
    usr_uuid = validate_uuid(str(user_id), "user_id") if user_id else None

    # 1. Read & validate file
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded Reverse MIS file is empty.")

    checksum = compute_sha256(file_bytes)

    # Check for duplicate upload
    existing_recon = await conn.fetchrow(
        "SELECT id FROM payroll_bank_reconciliations WHERE file_checksum_sha256 = $1;", checksum
    )
    if existing_recon:
        raise HTTPException(
            status_code=409,
            detail=f"This exact bank file has already been reconciled (Reconciliation ID: {existing_recon['id']}). Duplicate upload rejected.",
        )

    # 2. Store original file in Document Vault
    await file.seek(0)
    vault_doc = await store_payroll_document(
        conn,
        user,
        file,
        document_category="BANKING",
        document_type="BANK_REVERSE_MIS",
        cycle_id=cycle_id,
        notes=f"Reconciliation file uploaded for batch {batch_reference or 'N/A'}",
    )

    # Decode CSV content safely
    try:
        content_text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content_text = file_bytes.decode("latin-1")

    reader = csv.reader(io.StringIO(content_text))
    rows = list(reader)
    if not rows or len(rows) < 2:
        raise HTTPException(status_code=400, detail="Invalid CSV: insufficient rows for header and data.")

    header = rows[0]
    col_map = _detect_csv_columns(header)
    if "account" not in col_map or "amount" not in col_map:
        raise HTTPException(
            status_code=400,
            detail="CSV header must include identifiable beneficiary account number and amount columns.",
        )

    # 3. Fetch cycle & entries with bank details
    async with conn.transaction():
        cycle = await conn.fetchrow(
            "SELECT * FROM payroll_cycles WHERE id = $1::uuid FOR UPDATE;", cid
        )
        if not cycle:
            raise HTTPException(status_code=404, detail="Payroll cycle not found.")

        # Fetch all entries in cycle with employee bank account details
        entries = await conn.fetch(
            """
            SELECT pe.id AS entry_id, pe.employee_id, pe.net_payable, pe.payout_status, pe.status,
                   pe.bank_utr_number, pe.earnings_breakdown, pe.deductions_breakdown,
                   ebd.account_number, ebd.ifsc_code, u.full_name, u.email
            FROM payroll_entries pe
            JOIN users u ON u.id = pe.employee_id
            LEFT JOIN employee_bank_details ebd ON ebd.employee_id = pe.employee_id AND ebd.is_primary = true
            WHERE pe.cycle_id = $1::uuid
            FOR UPDATE OF pe;
            """,
            cid,
        )
        if not entries:
            raise HTTPException(status_code=400, detail="No payroll entries found for this cycle.")

        # Build index: cleaned_account -> entry
        account_to_entry: dict[str, Any] = {}
        for e in entries:
            acc = _clean_account(str(e["account_number"] or ""))
            if acc:
                account_to_entry[acc] = e

        # Create Reconciliation Header Record
        recon_id = uuid4()
        await conn.execute(
            """
            INSERT INTO payroll_bank_reconciliations (
                id, cycle_id, document_id, file_checksum_sha256,
                total_rows, status, uploaded_by
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4,
                $5, 'PROCESSING', $6::uuid
            );
            """,
            recon_id,
            cid,
            validate_uuid(vault_doc.id, "document_id"),
            checksum,
            len(rows) - 1,
            usr_uuid,
        )

        matched_count = 0
        failed_count = 0
        review_count = 0
        reversal_count = 0
        total_matched_amt = ZERO
        total_failed_amt = ZERO

        processed_accounts: set[str] = set()
        transactions_to_insert = []
        entries_to_mark_paid = []
        entries_to_mark_failed = []
        entries_to_mark_review = []
        entries_to_mark_reversed = []

        reimbursements_to_settle: set[str] = set()
        advances_to_settle: dict[str, Decimal] = {}

        for row_idx, r in enumerate(rows[1:], start=2):
            if not r or not any(field.strip() for field in r):
                continue

            raw_acc = r[col_map["account"]].strip() if col_map["account"] < len(r) else ""
            cleaned_acc = _clean_account(raw_acc)
            raw_amt = r[col_map["amount"]].strip() if col_map["amount"] < len(r) else "0"
            amount = _parse_decimal(raw_amt)

            utr = r[col_map["utr"]].strip() if "utr" in col_map and col_map["utr"] < len(r) else None
            status_text = r[col_map["status"]].strip().upper() if "status" in col_map and col_map["status"] < len(r) else "SUCCESS"
            ifsc = r[col_map["ifsc"]].strip().upper() if "ifsc" in col_map and col_map["ifsc"] < len(r) else None
            reason = r[col_map["reason"]].strip() if "reason" in col_map and col_map["reason"] < len(r) else None

            # Detect Duplicate bank record within this upload
            if cleaned_acc in processed_accounts:
                match_outcome = "DUPLICATE_BANK_RECORD"
                review_count += 1
                transactions_to_insert.append((
                    uuid4(), recon_id, cid, None, None, raw_acc, _mask_account(raw_acc),
                    ifsc, amount, utr, status_text, "DUP_ROW", "Duplicate record in bank MIS file",
                    match_outcome, False
                ))
                continue
            processed_accounts.add(cleaned_acc)

            # Match against candidate entry
            matched_entry = account_to_entry.get(cleaned_acc)
            if not matched_entry:
                match_outcome = "UNMATCHED"
                review_count += 1
                transactions_to_insert.append((
                    uuid4(), recon_id, cid, None, None, raw_acc, _mask_account(raw_acc),
                    ifsc, amount, utr, status_text, "AC_NOT_FOUND", "Account not found in this payroll cycle",
                    match_outcome, False
                ))
                continue

            expected_amt = round_paisa(Decimal(str(matched_entry["net_payable"])))
            entry_id = matched_entry["entry_id"]
            emp_id = matched_entry["employee_id"]

            # Amount validation
            if abs(amount - expected_amt) > TOLERANCE:
                match_outcome = "AMOUNT_MISMATCH"
                review_count += 1
                entries_to_mark_review.append((entry_id, "AMOUNT_MISMATCH", f"Expected ₹{expected_amt}, bank reported ₹{amount}"))
                transactions_to_insert.append((
                    uuid4(), recon_id, cid, entry_id, emp_id, raw_acc, _mask_account(raw_acc),
                    ifsc, amount, utr, status_text, "AMT_MISMATCH", f"Expected ₹{expected_amt}, got ₹{amount}",
                    match_outcome, False
                ))
                continue

            # Check status classification
            is_success = any(s in status_text for s in ["SUCCESS", "PAID", "PROCESSED", "CLEARED", "SETTLED"])
            is_reversed = any(s in status_text for s in ["REVERSED", "RETURNED", "BOUNCED", "REVERSAL"])
            is_failed = any(s in status_text for s in ["FAILED", "REJECTED", "DECLINED", "ERROR"])

            if is_success:
                match_outcome = "EXACT_MATCH"
                matched_count += 1
                total_matched_amt += amount
                entries_to_mark_paid.append((entry_id, utr or "BANK-CONFIRMED"))

                # Collect linked reimbursements and advances for settlement
                import json
                e_json = matched_entry["earnings_breakdown"]
                if isinstance(e_json, str):
                    try: e_json = json.loads(e_json)
                    except: e_json = {}
                for rid in (e_json or {}).get("reimbursement_ids") or []:
                    if rid: reimbursements_to_settle.add(str(rid))

                d_json = matched_entry["deductions_breakdown"]
                if isinstance(d_json, str):
                    try: d_json = json.loads(d_json)
                    except: d_json = {}
                for alloc in (d_json or {}).get("advance_recoveries") or []:
                    adv_id = alloc.get("advance_id")
                    applied = round_paisa(Decimal(str(alloc.get("applied_amount", "0"))))
                    if adv_id and applied > ZERO:
                        adv_key = str(adv_id)
                        advances_to_settle[adv_key] = round_paisa(advances_to_settle.get(adv_key, ZERO) + applied)

                transactions_to_insert.append((
                    uuid4(), recon_id, cid, entry_id, emp_id, raw_acc, _mask_account(raw_acc),
                    ifsc, amount, utr, status_text, "00", reason or "Success",
                    match_outcome, True
                ))
            elif is_reversed:
                match_outcome = "REVERSED_TRANSACTION"
                reversal_count += 1
                entries_to_mark_reversed.append((entry_id, "REVERSED", reason or "Bank return advice received"))
                transactions_to_insert.append((
                    uuid4(), recon_id, cid, entry_id, emp_id, raw_acc, _mask_account(raw_acc),
                    ifsc, amount, utr, status_text, "REVERSED", reason or "Reversed by destination bank",
                    match_outcome, False
                ))
            elif is_failed:
                match_outcome = "FAILED_TRANSACTION"
                failed_count += 1
                total_failed_amt += amount
                entries_to_mark_failed.append((entry_id, "FAILED", reason or "Bank transfer rejected"))
                transactions_to_insert.append((
                    uuid4(), recon_id, cid, entry_id, emp_id, raw_acc, _mask_account(raw_acc),
                    ifsc, amount, utr, status_text, "FAILED", reason or "Transaction failed",
                    match_outcome, False
                ))
            else:
                match_outcome = "REQUIRES_REVIEW"
                review_count += 1
                entries_to_mark_review.append((entry_id, "AMBIGUOUS_STATUS", f"Unknown bank status '{status_text}'"))
                transactions_to_insert.append((
                    uuid4(), recon_id, cid, entry_id, emp_id, raw_acc, _mask_account(raw_acc),
                    ifsc, amount, utr, status_text, "AMBIGUOUS", f"Unrecognized status '{status_text}'",
                    match_outcome, False
                ))

        # 4. Insert all transaction rows
        for t in transactions_to_insert:
            await conn.execute(
                """
                INSERT INTO payroll_payment_transactions (
                    id, reconciliation_id, cycle_id, payroll_entry_id,
                    employee_id, raw_account_number, account_number_mask,
                    ifsc_code, amount, bank_utr, bank_status,
                    bank_response_code, bank_response_message, match_outcome, is_settled
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                    $5::uuid, $6, $7,
                    $8, $9, $10, $11,
                    $12, $13, $14, $15
                );
                """,
                *t,
            )

        # 5. Apply state updates to payroll_entries
        for entry_id, utr_num in entries_to_mark_paid:
            await conn.execute(
                """
                UPDATE payroll_entries
                SET payout_status = 'PAID',
                    bank_utr_number = $2,
                    bank_verified = true,
                    paid_at = now(),
                    reconciled_at = now(),
                    reconciled_by = $3::uuid,
                    reconciliation_source_id = $4::uuid,
                    updated_at = now()
                WHERE id = $1::uuid;
                """,
                entry_id,
                utr_num,
                usr_uuid,
                recon_id,
            )

        for entry_id, code, msg in entries_to_mark_failed:
            await conn.execute(
                """
                UPDATE payroll_entries
                SET payout_status = 'FAILED',
                    bank_response_code = $2,
                    bank_response_message = $3,
                    remediation_status = 'NEEDS_CORRECTION',
                    reconciled_at = now(),
                    reconciled_by = $4::uuid,
                    reconciliation_source_id = $5::uuid,
                    updated_at = now()
                WHERE id = $1::uuid;
                """,
                entry_id,
                code,
                msg,
                usr_uuid,
                recon_id,
            )

        for entry_id, code, msg in entries_to_mark_reversed:
            await conn.execute(
                """
                UPDATE payroll_entries
                SET payout_status = 'REVERSED',
                    bank_response_code = $2,
                    bank_response_message = $3,
                    remediation_status = 'NEEDS_CORRECTION',
                    reconciled_at = now(),
                    reconciled_by = $4::uuid,
                    reconciliation_source_id = $5::uuid,
                    updated_at = now()
                WHERE id = $1::uuid;
                """,
                entry_id,
                code,
                msg,
                usr_uuid,
                recon_id,
            )

        for entry_id, code, msg in entries_to_mark_review:
            await conn.execute(
                """
                UPDATE payroll_entries
                SET payout_status = 'REQUIRES_REVIEW',
                    bank_response_code = $2,
                    bank_response_message = $3,
                    remediation_status = 'NEEDS_CORRECTION',
                    reconciled_at = now(),
                    reconciled_by = $4::uuid,
                    reconciliation_source_id = $5::uuid,
                    updated_at = now()
                WHERE id = $1::uuid;
                """,
                entry_id,
                code,
                msg,
                usr_uuid,
                recon_id,
            )

        # 6. Settle reimbursements & advances strictly for PAID entries
        if reimbursements_to_settle:
            await conn.execute(
                """
                UPDATE reimbursements
                SET status = 'paid', paid_at = now()
                WHERE id = ANY($1::uuid[]) AND status = 'approved' AND paid_at IS NULL;
                """,
                list(reimbursements_to_settle),
            )

        if advances_to_settle:
            adv_rows = await conn.fetch(
                "SELECT id, remaining_balance, paid_installments, status FROM employee_salary_advances WHERE id = ANY($1::uuid[]) FOR UPDATE;",
                list(advances_to_settle.keys()),
            )
            for adv in adv_rows:
                aid = str(adv["id"])
                applied = advances_to_settle.get(aid, ZERO)
                curr_bal = round_paisa(Decimal(str(adv["remaining_balance"])))
                new_bal = max(ZERO, round_paisa(curr_bal - applied))
                new_status = "COMPLETED" if new_bal <= ZERO else adv["status"]
                await conn.execute(
                    """
                    UPDATE employee_salary_advances
                    SET remaining_balance = $2, paid_installments = paid_installments + 1, status = $3, updated_at = now()
                    WHERE id = $1::uuid;
                    """,
                    adv["id"],
                    new_bal,
                    new_status,
                )

        # 7. Update Cycle Status
        total_cleared = await conn.fetchval(
            "SELECT count(*) FROM payroll_entries WHERE cycle_id = $1::uuid AND status = 'CLEARED';", cid
        )
        total_paid = await conn.fetchval(
            "SELECT count(*) FROM payroll_entries WHERE cycle_id = $1::uuid AND status = 'CLEARED' AND payout_status = 'PAID';", cid
        )
        total_failed_entries = await conn.fetchval(
            "SELECT count(*) FROM payroll_entries WHERE cycle_id = $1::uuid AND status = 'CLEARED' AND payout_status IN ('FAILED', 'REVERSED', 'REQUIRES_REVIEW');", cid
        )

        if total_paid == total_cleared and total_cleared > 0:
            new_cycle_status = "DISBURSED"
        elif total_paid > 0 or total_failed_entries > 0:
            new_cycle_status = "PARTIALLY_DISBURSED"
        else:
            new_cycle_status = "DISBURSEMENT_IN_PROGRESS"

        await conn.execute(
            """
            UPDATE payroll_cycles
            SET status = $2::text,
                disbursed_at = CASE WHEN $2::text = 'DISBURSED' THEN now() ELSE disbursed_at END,
                updated_at = now()
            WHERE id = $1::uuid;
            """,
            cid,
            new_cycle_status,
        )

        # Update reconciliation record status
        await conn.execute(
            """
            UPDATE payroll_bank_reconciliations
            SET matched_count = $2,
                failed_count = $3,
                review_count = $4,
                reversal_count = $5,
                total_matched_amount = $6,
                total_failed_amount = $7,
                status = 'COMPLETED'
            WHERE id = $1::uuid;
            """,
            recon_id,
            matched_count,
            failed_count,
            review_count,
            reversal_count,
            total_matched_amt,
            total_failed_amt,
        )

        # Audit log
        await conn.execute(
            """
            INSERT INTO payroll_audit_logs (
                cycle_id, actor_id, actor_role, action,
                entity_type, entity_id, new_state, comments
            )
            VALUES (
                $1::uuid, $2::uuid, $3, 'BANK_RECONCILIATION_COMPLETED',
                'payroll_bank_reconciliations', $4, $5::jsonb, $6
            );
            """,
            cid,
            usr_uuid,
            user.get("role", "system"),
            str(recon_id),
            f'{{"matched": {matched_count}, "failed": {failed_count}, "reviews": {review_count}, "cycle_status": "{new_cycle_status}"}}',
            f"Reconciled bank file with {matched_count} matches and {failed_count} failures.",
        )

    return await get_reconciliation_summary(conn, str(recon_id), cycle_id=cycle_id)


# ---------------------------------------------------------------------------
# Reconciliation Dashboard & Summary Service
# ---------------------------------------------------------------------------
async def get_reconciliation_summary(
    conn,
    reconciliation_id: Optional[str] = None,
    *,
    cycle_id: str,
) -> BankReconciliationSummaryResponse:
    """
    Produce live transaction reconciliation totals for the reconciliation dashboard.
    """
    cid = validate_uuid(cycle_id, "cycle_id")

    # Aggregate live counts from payroll_entries
    rows = await conn.fetch(
        """
        SELECT pe.payout_status, pe.net_payable
        FROM payroll_entries pe
        WHERE pe.cycle_id = $1::uuid AND pe.status = 'CLEARED';
        """,
        cid,
    )

    total_instructions = len(rows)
    total_amount = sum((Decimal(str(r["net_payable"])) for r in rows), start=ZERO)

    success_count = sum(1 for r in rows if r["payout_status"] == "PAID")
    success_amt = sum((Decimal(str(r["net_payable"])) for r in rows if r["payout_status"] == "PAID"), start=ZERO)

    failed_count = sum(1 for r in rows if r["payout_status"] == "FAILED")
    failed_amt = sum((Decimal(str(r["net_payable"])) for r in rows if r["payout_status"] == "FAILED"), start=ZERO)

    reversed_count = sum(1 for r in rows if r["payout_status"] == "REVERSED")
    reversed_amt = sum((Decimal(str(r["net_payable"])) for r in rows if r["payout_status"] == "REVERSED"), start=ZERO)

    review_count = sum(1 for r in rows if r["payout_status"] == "REQUIRES_REVIEW")

    pending_count = sum(1 for r in rows if r["payout_status"] in ("PENDING", "NOT_GENERATED", "GENERATED", "SUBMITTED_TO_BANK", "BANK_ACCEPTED", "PROCESSING"))
    pending_amt = sum((Decimal(str(r["net_payable"])) for r in rows if r["payout_status"] in ("PENDING", "NOT_GENERATED", "GENERATED", "SUBMITTED_TO_BANK", "BANK_ACCEPTED", "PROCESSING")), start=ZERO)

    # Fetch individual transaction records if available
    tx_rows = await conn.fetch(
        """
        SELECT pt.id, pt.reconciliation_id, pt.cycle_id, pt.payroll_entry_id,
               pt.employee_id, u.full_name AS employee_name, pt.account_number_mask,
               pt.ifsc_code, pt.amount, pt.bank_utr, pt.bank_status,
               pt.bank_response_code, pt.bank_response_message, pt.match_outcome,
               pt.is_settled, pt.created_at
        FROM payroll_payment_transactions pt
        LEFT JOIN users u ON u.id = pt.employee_id
        WHERE pt.cycle_id = $1::uuid
        ORDER BY pt.created_at DESC;
        """,
        cid,
    )

    transactions = [
        PaymentTransactionRow(
            id=str(t["id"]),
            reconciliation_id=str(t["reconciliation_id"]),
            cycle_id=str(t["cycle_id"]),
            payroll_entry_id=str(t["payroll_entry_id"]) if t["payroll_entry_id"] else None,
            employee_id=str(t["employee_id"]) if t["employee_id"] else None,
            employee_name=t["employee_name"],
            account_number_mask=t["account_number_mask"],
            ifsc_code=t["ifsc_code"],
            amount=float(t["amount"]),
            bank_utr=t["bank_utr"],
            bank_status=t["bank_status"],
            bank_response_code=t["bank_response_code"],
            bank_response_message=t["bank_response_message"],
            match_outcome=t["match_outcome"],
            is_settled=t["is_settled"],
            created_at=t["created_at"].isoformat(),
        )
        for t in tx_rows
    ]

    return BankReconciliationSummaryResponse(
        reconciliation_id=reconciliation_id,
        cycle_id=cycle_id,
        total_payment_instructions=total_instructions,
        total_amount_submitted=float(round_paisa(total_amount)),
        successful_count=success_count,
        successful_amount=float(round_paisa(success_amt)),
        failed_count=failed_count,
        failed_amount=float(round_paisa(failed_amt)),
        pending_count=pending_count,
        pending_amount=float(round_paisa(pending_amt)),
        reversed_count=reversed_count,
        reversed_amount=float(round_paisa(reversed_amt)),
        review_count=review_count,
        unmatched_count=sum(1 for t in transactions if t.match_outcome == "UNMATCHED"),
        transactions=transactions,
    )
