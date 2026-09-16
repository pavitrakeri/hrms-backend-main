import re
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior)\s+instructions",
    r"you\s+are\s+now\s+(DAN|unfiltered|jailbroken)",
    r"system\s+prompt\s+override",
    r"reveal\s+(the\s+)?(system\s+prompt|api\s+key|database\s+password)",
    r"bypass\s+(all\s+)?(rules|safety|guardrails)",
    r"drop\s+table",
    r"delete\s+from\s+users",
]

def sanitize_user_input(text: str) -> str:
    """Checks user text for prompt injection markers and normalizes it."""
    clean_text = text.strip()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, clean_text, re.IGNORECASE):
            logger.warning(f"Potential prompt injection detected: {pattern}")
            return "[Message filtered due to security policy violation]"
    return clean_text

def sanitize_tool_output(data: Any, caller: Dict[str, Any]) -> Any:
    """Ensures sensitive cross-employee data is not leaked."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if k in ("password_hash", "token", "secret", "jwt"):
                continue
            # If regular employee is looking at someone else's record, redact salary
            if k in ("basic_salary", "total_salary", "hra", "bank_account_number", "iban_number"):
                target_user_id = data.get("user_id") or data.get("id") or data.get("employee_id")
                if target_user_id and str(target_user_id) != str(caller.get("id")) and caller.get("role") not in ("admin", "hr", "cfo"):
                    sanitized[k] = "[REDACTED]"
                    continue
            sanitized[k] = sanitize_tool_output(v, caller)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_tool_output(item, caller) for item in data]
    return data
