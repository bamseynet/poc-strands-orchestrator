"""Keep AWS account details out of error text shown to users of a public demo."""

import re

_ARN = re.compile(r"arn:aws[a-z-]*:[^\s'\",]+")
_ACCOUNT_ID = re.compile(r"\b\d{12}\b")
_MAX_ERROR_CHARS = 500


def redact(message: str) -> str:
    """Strip ARNs and account ids, and bound the length. The full error stays in server logs."""
    cleaned = _ACCOUNT_ID.sub("[account]", _ARN.sub("[arn]", message))
    return cleaned[:_MAX_ERROR_CHARS]
