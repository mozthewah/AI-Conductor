"""Copilot CLI's PermissionRequest hook -> Conductor's safety check.

Copilot runs this for each action that needs permission. It forwards the request to the Conductor run
that started Copilot (CONDUCTOR_HOOK_URL) and prints Copilot's decision format.

Outside a Conductor run (CONDUCTOR_HOOK_URL unset) it prints `{}`: no decision, so your own Copilot
sessions behave exactly as before. Inside a Conductor run, if Conductor can't be reached it denies
(fail closed).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> None:
    url = os.environ.get("CONDUCTOR_HOOK_URL", "")
    if not url:
        print("{}")
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3600) as r:
            out = json.loads(r.read() or b"{}").get("hookSpecificOutput") or {}
        allowed = out.get("permissionDecision") == "allow"
        reason = out.get("permissionDecisionReason") or ("" if allowed else "Declined by the user's safety settings.")
    except Exception as e:
        allowed, reason = False, f"Conductor couldn't check this action ({type(e).__name__})."
    print(json.dumps({"behavior": "allow"} if allowed else {"behavior": "deny", "message": reason}))


if __name__ == "__main__":
    main()
