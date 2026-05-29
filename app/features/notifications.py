# app/features/notifications.py
import os
import json
import smtplib
import urllib.request
import urllib.parse
import logging
from email.message import EmailMessage
from fastapi import BackgroundTasks
from typing import Dict

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", os.getenv("FROM_EMAIL", SMTP_USER or "noreply@aimploy.org"))

MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")
MS_TENANT_ID = os.getenv("MS_TENANT_ID")

async def create_db_notification(conn, user_id: str, ntype: str, payload: Dict):
    await conn.execute(
        "INSERT INTO notifications (id, user_id, type, payload) VALUES (gen_random_uuid(), $1, $2, $3)",
        user_id, ntype, json.dumps(payload)
    )

def _send_email_sync(to_email: str, subject: str, body: str):
    # Check if Microsoft Graph credentials are set
    if MS_CLIENT_ID and MS_CLIENT_SECRET and MS_TENANT_ID:
        try:
            logger.info("Attempting to send welcome email via Microsoft Graph API to %s", to_email)
            # 1. Get access token from Microsoft Identity Platform
            token_url = f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token"
            token_data = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": MS_CLIENT_ID,
                "client_secret": MS_CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default"
            }).encode("utf-8")
            
            token_req = urllib.request.Request(
                token_url,
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST"
            )
            with urllib.request.urlopen(token_req, timeout=15) as res:
                token_resp = json.loads(res.read().decode("utf-8"))
                access_token = token_resp["access_token"]
                
            # 2. Send email via Microsoft Graph API
            send_url = f"https://graph.microsoft.com/v1.0/users/{FROM_EMAIL}/sendMail"
            email_payload = {
                "message": {
                    "subject": subject,
                    "body": {
                        "contentType": "Text",
                        "content": body
                    },
                    "toRecipients": [
                        {
                            "emailAddress": {
                                "address": to_email
                            }
                        }
                    ]
                },
                "saveToSentItems": "false"
            }
            email_data = json.dumps(email_payload).encode("utf-8")
            email_req = urllib.request.Request(
                send_url,
                data=email_data,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            with urllib.request.urlopen(email_req, timeout=15) as res:
                if res.status in (200, 202):
                    logger.info("Email successfully sent via Microsoft Graph API to %s", to_email)
                    return True
                else:
                    logger.error("Microsoft Graph API email send failed with status: %s", res.status)
                    return False
        except Exception as e:
            logger.exception("Microsoft Graph API email send failed")
            return False

    # Fallback to standard SMTP
    logger.info("Microsoft Graph credentials not fully set. Falling back to SMTP for %s", to_email)
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
            logger.info("Email successfully sent via SMTP to %s", to_email)
            return True
    except Exception as e:
        logger.exception("SMTP Email send failed")
        return False

def send_email_background(bg: BackgroundTasks, to_email: str, subject: str, body: str):
    if bg:
        bg.add_task(_send_email_sync, to_email, subject, body)
