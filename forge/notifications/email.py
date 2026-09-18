"""Optional completion email delivery for autonomous Forge runs.

No email is sent unless the operator explicitly configures the Resend
environment variables. Missing configuration is a no-op.
"""
from __future__ import annotations

import json
import os
from urllib import request


def notify_build_complete(*, project_id: str, build_id: str, stages: list) -> bool:
    api_key = os.environ.get("FORGE_RESEND_API_KEY", "").strip()
    recipient = os.environ.get("FORGE_COMPLETION_EMAIL_TO", "").strip()
    sender = os.environ.get("FORGE_COMPLETION_EMAIL_FROM", "").strip()
    if not (api_key and recipient and sender):
        return False

    passed = sum(
        1 for item in stages
        if str(item.get("status", "")).lower() == "passed"
    )
    subject = "Forge build completed: %s" % build_id
    body = (
        "Forge completed an autonomous staged build.\n\n"
        "Project: %s\nBuild: %s\nStages passed: %d/%d\n"
        % (project_id, build_id, passed, len(stages))
    )
    payload = json.dumps({
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "text": body,
    }).encode("utf-8")

    req = request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=15) as response:
            return 200 <= response.status < 300
    except Exception:
        return False
