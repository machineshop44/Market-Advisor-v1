#!/usr/bin/env python3
"""Remote desk check-in — run from any machine with monitor URL + token."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cursor_monitor as cm


def main() -> int:
    p = argparse.ArgumentParser(description="Remote Market Advisor desk snag check")
    p.add_argument("--url", default=os.environ.get("MARKET_ADVISOR_URL", ""))
    p.add_argument("--token", default=os.environ.get("MARKET_ADVISOR_TOKEN", ""))
    p.add_argument("--user", default=os.environ.get("MARKET_ADVISOR_USER", ""))
    p.add_argument("--pass", dest="password", default=os.environ.get("MARKET_ADVISOR_PASS", ""))
    p.add_argument("--json", action="store_true", help="Print raw JSON")
    p.add_argument("--full", action="store_true", help="Include digest + snags")
    args = p.parse_args()
    conn = {
        "url": args.url or "https://127.0.0.1:8791",
        "token": args.token,
        "user": args.user,
        "password": args.password,
        "verify_tls": False,
    }
    if args.full:
        digest = cm.fetch_digest(conn)
        snags = cm.fetch_snags(conn)
        if args.json:
            print(json.dumps({"digest": digest, "snags": snags}, indent=2, default=str))
        else:
            if digest.get("summary_text"):
                print(digest.get("summary_text"))
            print()
            print(cm.format_snag_report(snags))
        err = digest.get("error") or snags.get("error")
        return 2 if err else (1 if (snags.get("status") in ("warn", "critical")) else 0)
    snags = cm.fetch_snags(conn)
    if args.json:
        print(json.dumps(snags, indent=2, default=str))
    else:
        print(cm.format_snag_report(snags))
    if snags.get("error"):
        return 2
    return 1 if snags.get("status") in ("warn", "critical") else 0


if __name__ == "__main__":
    raise SystemExit(main())
