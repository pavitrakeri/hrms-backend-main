# app/features/notifications.py
import os
import json
import smtplib
from email.message import EmailMessage
from fastapi import BackgroundTasks
from typing import Dict

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)

async def create_db_notification(conn, user_id: str, ntype: str, payload: Dict):
    await conn.execute(
        "INSERT INTO notifications (id, user_id, type, payload) VALUES (gen_random_uuid(), $1, $2, $3)",
        user_id, ntype, json.dumps(payload)
    )

def _send_email_sync(to_email: str, subject: str, body: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        # In prod, log this instead of printing
        print("Email send failed:", e)

def send_email_background(bg: BackgroundTasks, to_email: str, subject: str, body: str):
    if bg:
        bg.add_task(_send_email_sync, to_email, subject, body)
