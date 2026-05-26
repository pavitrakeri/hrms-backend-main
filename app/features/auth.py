import os
import jwt
from datetime import datetime, timedelta
import bcrypt
from pydantic import BaseModel, EmailStr
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import asyncpg
from app.db import get_db_pool
from app.datamodels.login import LoginRequest, TokenResponse

security = HTTPBearer()


JWT_SECRET = os.getenv("JWT_SECRET", "changeme")
JWT_ALGO = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440 # 24 hours in minutes

# --- Password verification using bcrypt directly ---
def verify_password(plain: str, hashed: str) -> bool:
    """
    plain: plaintext password from user
    hashed: stored bcrypt hash from DB
    """
    return bcrypt.checkpw(plain.encode(), hashed.encode())

def create_access_token(user_id: str, role: str, email: str):
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": user_id, "role": role, "email": email, "exp": expire}
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)
    return token, expire



async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(security)
):
    token = creds.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="User_id not found")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT u.id, u.email, r.name as role
            FROM users u
            LEFT JOIN roles r ON u.role_id = r.id
            WHERE u.id=$1 AND u.is_active=true
        """, user_id)

    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    return {"id": str(row["id"]), "email": row["email"], "role": row["role"]}


async def reset_password(conn, req):
    """
    Allow user to reset their password using email.
    """
    # 1️⃣ Check passwords match
    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    # 2️⃣ Check user exists
    user = await conn.fetchrow("SELECT id FROM users WHERE email=$1", req.email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # 3️⃣ Hash new password
    pw_hash = bcrypt.hashpw(req.new_password.encode("utf-8"), bcrypt.gensalt()).decode()

    # 4️⃣ Update password in DB
    await conn.execute(
        "UPDATE users SET password_hash=$1 WHERE id=$2",
        pw_hash, user["id"]
    )

    return {"status": "success", "message": "Password updated successfully"}
