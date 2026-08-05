from typing import List

# List of available permissions
MANAGE_EMPLOYEES = "manage_employees"
MANAGE_ROLES = "manage_roles"
MANAGE_DEPARTMENTS = "manage_departments"
MANAGE_LEAVES = "manage_leaves"
MANAGE_PAYROLL = "manage_payroll"
MANAGE_POLICIES = "manage_policies"
MANAGE_RECRUITMENT = "manage_recruitment"

AVAILABLE_PERMISSIONS = [
    MANAGE_EMPLOYEES,
    MANAGE_ROLES,
    MANAGE_DEPARTMENTS,
    MANAGE_LEAVES,
    MANAGE_PAYROLL,
    MANAGE_POLICIES,
    MANAGE_RECRUITMENT
]

def has_permission(user: dict, permission: str) -> bool:
    """
    Check if the user has the required permission.
    Admin always has full access.
    """
    if user.get("role") == "admin":
        return True
    
    user_permissions = user.get("permissions", [])
    if not isinstance(user_permissions, list):
        return False
        
    return permission in user_permissions

def require_permission(user: dict, permission: str):
    """
    Raise an exception if the user does not have the required permission.
    Use this in routes instead of `role not in (...)`.
    """
    from fastapi import HTTPException
    
    if not has_permission(user, permission):
        raise HTTPException(status_code=403, detail=f"Not authorized. Missing permission: {permission}")
