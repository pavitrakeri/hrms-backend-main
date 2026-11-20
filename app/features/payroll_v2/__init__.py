"""
Payroll V2 — Indian Statutory Payroll Module
Clean-slate implementation decoupled from legacy payroll code.

Submodules:
  - engine_india.py : Pure Decimal calculation core (EPF, EPS, ESI, PT, Gratuity, TDS)
  - adapters/       : Read-only upstream ingestion (calendar, leaves, attendance, expenses, advances)
  - master_service  : Corporate statutory, salary structure, bank, tax decl, and loan masters
  - cycle_service   : Workflow state machine, line-item hold, 3-tier approvals, disbursement, ESS
  - rbac            : Strict role normalization and 3-tier approval segregation helpers
"""

from app.features.payroll_v2 import cycle_service, engine_india, master_service, rbac

__all__ = ["cycle_service", "engine_india", "master_service", "rbac"]
