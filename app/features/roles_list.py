from fastapi import HTTPException

async def list_roles(conn, user):
    """
    List all roles.
    Accessible to Admin and HR only.
    """
    
    # Check permission
    from app.utils.permissions import require_permission, MANAGE_ROLES
    require_permission(user, MANAGE_ROLES)

    # Fetch all roles
    rows = await conn.fetch("""
        SELECT id, name, description, permissions
        FROM roles
        ORDER BY name ASC
    """)
    
    import json
    
    result = []
    for row in rows:
        permissions = []
        if row["permissions"]:
            if isinstance(row["permissions"], str):
                try:
                    permissions = json.loads(row["permissions"])
                except:
                    pass
            else:
                permissions = row["permissions"]
                
        result.append({
            "id": str(row["id"]),
            "name": row["name"],
            "description": row["description"],
            "permissions": permissions
        })

    return result
