"""Export E*TRADE keyring secrets to JSON for portable session restore."""
import json
import os
import sys

try:
    import keyring
except ImportError:
    sys.exit(0)

svc = "MarketAdvisor.ETrade"
secrets = {}
for env in ("sandbox", "live"):
    for kind in (
        "access_token",
        "access_token_secret",
        "request_token",
        "request_token_secret",
    ):
        name = f"{env}:{kind}"
        try:
            v = keyring.get_password(svc, name)
        except Exception:
            v = None
        if v:
            secrets[name] = v

out = os.environ.get("MA_ETRADE_EXPORT_JSON") or ""
if secrets and out:
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"service": svc, "secrets": secrets}, f, indent=2)
    print("Exported", len(secrets), "E*TRADE keyring secret(s)")
