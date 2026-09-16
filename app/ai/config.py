import os
import logging
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-004")

_client = None

def get_gemini_client():
    global _client
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not configured in environment.")
        return None
    if _client is None:
        try:
            from google import genai
            _client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.error(f"Failed to initialize Google GenAI client: {e}")
            return None
    return _client

SYSTEM_INSTRUCTION = """
You are Aimploy Assistant, the intelligent AI HR and Operations Assistant for Aimploy.
Your duty is to assist the authenticated employee, manager, or HR professional accurately, concisely, and securely.

Operating Rules:
1. Ground your answers in company policies and verified Aimploy data.
2. Respect Role-Based Access: Never disclose another employee's private personal information, salary details, or bank credentials.
3. For mutating actions (such as applying for a leave or submitting claims), use the provided tool to propose the action. An explicit confirmation card will be shown to the user before committing any data changes.
4. If a query requires data, always invoke the appropriate tool rather than fabricating facts.
5. Maintain a professional, helpful, and empathetic tone.
"""
