"""
Role-Based Access Control (RBAC) & Segregation-of-Duties Helpers for Payroll V2.

Normalizes database role labels (`hr` / `hr_admin`, `finance` / `finance_manager`,
`cfo`, `admin`, `employee`) and supports Aimploy's `manage_payroll` permission flag
while enforcing strict 3-tier approval segregation:
  Step 1 (`hr_admin`)        -> HR / Admin
  Step 2 (`finance_manager`) -> Finance / Admin
  Step 3 (`cfo`)             -> CFO / Admin
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from fastapi import HTTPException

from app.utils.permissions import MANAGE_PAYROLL, has_permission

_ROLE_ALIASES: dict[str, str] = {
    "admin": "admin",
    "super_admin": "admin",
    "system_admin": "admin",
    "hr": "hr",
    "hr_admin": "hr",
    "hr_manager": "hr",
    "human_resources": "hr",
    "finance": "finance",
    "finance_manager": "finance",
    "accounts": "finance",
    "accountant": "finance",
    "cfo": "cfo",
    "chief_financial_officer": "cfo",
    "employee": "employee",
    "manager": "manager",
    "line_manager": "manager",
}

APPROVAL_CHAIN_STEPS: tuple[tuple[int, str], ...] = (
    (1, "hr_admin"),
    (2, "finance_manager"),
    (3, "cfo"),
)

_STEP_ALLOWED_CANONICAL_ROLES: dict[str, frozenset[str]] = {
    "hr_admin": frozenset({"hr", "admin"}),
    "finance_manager": frozenset({"finance", "admin"}),
    "cfo": frozenset({"cfo", "admin"}),
}


def get_normalized_role(user: dict[str, Any]) -> str:
    raw = str(user.get("role") or "").strip().lower()
    return _ROLE_ALIASES.get(raw, raw)


def is_hr_or_admin(user: dict[str, Any]) -> bool:
    role = get_normalized_role(user)
    return role in {"hr", "admin"} or has_permission(user, MANAGE_PAYROLL)


def require_payroll_roles(
    user: dict[str, Any],
    allowed_roles: Iterable[str],
    *,
    allow_manage_payroll_perm: bool = False,
) -> str:
    """
    Validate caller holds one of `allowed_roles` (canonical role keys).
    Returns normalized role name on success; raises HTTP 403 otherwise.
    """
    canonical = get_normalized_role(user)
    allowed_set = {r.strip().lower() for r in allowed_roles}

    if canonical in allowed_set:
        return canonical

    if allow_manage_payroll_perm and has_permission(user, MANAGE_PAYROLL):
        return canonical

    raise HTTPException(
        status_code=403,
        detail=(
            f"Not authorized for this payroll action. "
            f"Required role: {', '.join(sorted(allowed_set))}."
        ),
    )


def require_approval_step_role(
    user: dict[str, Any],
    *,
    step_order: int,
    role_required: str,
) -> str:
    """
    Enforce sequential 3-tier segregation of duties on `payroll_cycle_approvals`.
    """
    canonical = get_normalized_role(user)
    allowed = _STEP_ALLOWED_CANONICAL_ROLES.get(role_required)

    if allowed is None:
        # Fallback if a custom role string appears
        allowed = frozenset({_ROLE_ALIASES.get(role_required.lower(), role_required.lower()), "admin"})

    if canonical in allowed:
        return canonical

    # Custom role with explicit MANAGE_PAYROLL is only permitted on HR prep Step 1
    # Segregation of Duties: CFO and Finance roles cannot action Step 1 even if they hold manage_payroll
    if role_required == "hr_admin" and has_permission(user, MANAGE_PAYROLL) and canonical not in {"cfo", "finance"}:
        return canonical

    raise HTTPException(
        status_code=403,
        detail=(
            f"Approval step {step_order} requires '{role_required}' authorization. "
            f"Your current role ('{user.get('role')}') cannot action this tier."
        ),
    )


def require_self_or_roles(
    user: dict[str, Any],
    target_employee_id: str,
    allowed_roles: Iterable[str],
) -> None:
    if str(user.get("id")) == str(target_employee_id):
        return
    require_payroll_roles(user, allowed_roles, allow_manage_payroll_perm=True)
