#!/usr/bin/env python3
"""Scan CPA xAI auths via management API; classify invalid/exhausted; optional quarantine.

Quota (free-usage-exhausted) is a rolling 24h window from xAI — NOT permanently dead.
  tokens limit ≈ 2_000_000 / 24h window (from live error text).

Usage:
  python3 cpa_auth_scan.py              # scan + write reports
  python3 cpa_auth_scan.py --quarantine # set disabled=true on spending (keep file)
  python3 cpa_auth_scan.py --revive     # re-enable disabled that look recovered / aged
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CPA_AUTH = Path("/vol1/1000/openzl/cpa/auths")
SECRETS = Path("/vol1/1000/openzl/cpa/.secrets.env")
STATE = Path("/vol1/1000/openzl/cpa/monitor/auth_invalid_report.json")
QUAR_META = Path("/vol1/1000/openzl/cpa/monitor/quarantine_meta.json")
MGMT_URL = "http://127.0.0.1:8317/v0/management/auth-files"

# free-usage-exhausted: wait at least this before force-revive attempt
SPENDING_COOLDOWN_H = float(os.environ.get("CPA_SPENDING_COOLDOWN_H", "20"))


def load_secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in SECRETS.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def fetch_auth_files(mgmt_key: str) -> list[dict]:
    req = urllib.request.Request(
        MGMT_URL,
        headers={
            "Authorization": f"Bearer {mgmt_key}",
            "User-Agent": "cpa-auth-scan/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    return list(data.get("files") or [])


def tag_message(msg: str) -> str:
    m = (msg or "").lower()
    if "free-usage-exhausted" in m or "spending-limit" in m or "run out of credits" in m:
        return "spending_exhausted"
    if "permission-denied" in m:
        return "permission_denied"
    if "rate" in m and "limit" in m:
        return "rate_limit"
    if msg:
        return "other_error"
    return "none"


def parse_token_usage(msg: str) -> dict | None:
    """Extract actual/limit from free-usage message if present."""
    import re

    m = re.search(r"tokens\s*\(actual/limit\):\s*(\d+)\s*/\s*(\d+)", msg or "")
    if not m:
        return None
    return {"actual": int(m.group(1)), "limit": int(m.group(2))}


def classify(files: list[dict]) -> dict:
    xai = [f for f in files if str(f.get("id") or "").startswith("xai-")]
    groups: dict[str, list] = defaultdict(list)
    for f in xai:
        msg = f.get("status_message") or ""
        t = tag_message(msg)
        item = {
            "email": f.get("email"),
            "id": f.get("id"),
            "status": f.get("status"),
            "unavailable": bool(f.get("unavailable")),
            "disabled": bool(f.get("disabled")),
            "failed": int(f.get("failed") or 0),
            "success": int(f.get("success") or 0),
            "status_message": (msg or "")[:240],
            "tag": t,
            "token_usage": parse_token_usage(msg),
            "last_refresh": f.get("last_refresh"),
            "updated_at": f.get("updated_at"),
        }
        groups[t].append(item)

    unavail = [i for g in groups.values() for i in g if i["unavailable"]]
    usable = [
        i
        for g in groups.values()
        for i in g
        if not i["unavailable"]
        and not i["disabled"]
        and i["tag"] != "spending_exhausted"
    ]
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "total": len(xai),
        "usable": len(usable),
        "unavailable": len(unavail),
        "by_tag": {k: len(v) for k, v in groups.items()},
        "groups": dict(groups),
        "unavailable_list": sorted(unavail, key=lambda x: -x["failed"]),
        "spending_list": groups.get("spending_exhausted") or [],
        "usable_estimate_note": (
            "usable = not unavailable, not disabled, not free-usage-exhausted. "
            "permission-denied may recover; spending resets on rolling 24h window."
        ),
    }


def load_quar_meta() -> dict:
    if QUAR_META.exists():
        try:
            return json.loads(QUAR_META.read_text())
        except Exception:
            pass
    return {"accounts": {}}


def save_quar_meta(meta: dict) -> None:
    QUAR_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


def set_disabled(auth_id: str, disabled: bool, reason: str) -> bool:
    path = CPA_AUTH / auth_id
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except Exception:
        return False
    data["disabled"] = bool(disabled)
    # non-standard note fields — CPA ignores unknown keys
    data["disabled_reason"] = reason if disabled else ""
    data["disabled_at"] = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if disabled else ""
    )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True


def quarantine_spending(report: dict) -> dict:
    meta = load_quar_meta()
    accounts = meta.setdefault("accounts", {})
    done = []
    for item in report.get("spending_list") or []:
        aid = item.get("id")
        if not aid:
            continue
        ok = set_disabled(aid, True, "spending_exhausted_rolling_24h")
        if ok:
            accounts[aid] = {
                "email": item.get("email"),
                "reason": "spending_exhausted",
                "quarantined_at": datetime.now(timezone.utc).isoformat(),
                "token_usage": item.get("token_usage"),
                "status_message": item.get("status_message"),
            }
            done.append(item.get("email"))
    meta["last_quarantine"] = datetime.now().isoformat(timespec="seconds")
    save_quar_meta(meta)
    return {"quarantined": done, "count": len(done)}


def revive_if_due(report: dict, force: bool = False) -> dict:
    """Re-enable disabled auths after cooldown; also clear local disabled on permission if not unavailable."""
    meta = load_quar_meta()
    accounts = meta.setdefault("accounts", {})
    now = datetime.now(timezone.utc)
    revived = []
    kept = []

    # map current mgmt state
    by_id = {}
    for g in (report.get("groups") or {}).values():
        for item in g:
            if item.get("id"):
                by_id[item["id"]] = item

    for path in CPA_AUTH.glob("xai-*.json"):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not data.get("disabled"):
            continue
        aid = path.name
        reason = (data.get("disabled_reason") or accounts.get(aid, {}).get("reason") or "")
        qat = accounts.get(aid, {}).get("quarantined_at") or data.get("disabled_at")
        age_h = None
        if qat:
            try:
                t = datetime.fromisoformat(qat.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                age_h = (now - t).total_seconds() / 3600
            except Exception:
                age_h = None

        mg = by_id.get(aid) or {}
        still_spending = mg.get("tag") == "spending_exhausted"
        still_unavail = bool(mg.get("unavailable"))

        should = False
        why = ""
        # Spending quarantine is ONLY lifted by cooldown (rolling 24h window).
        # Do NOT trust empty status_message / mgmt_cleared — CPA often clears the
        # last error after a file rewrite, which would re-enable dead-quota accounts
        # within seconds and waste retries.
        if force:
            should, why = True, "force"
        elif reason.startswith("spending"):
            if age_h is not None and age_h >= SPENDING_COOLDOWN_H:
                should, why = True, f"cooldown_{age_h:.1f}h"
            else:
                should = False
        elif reason.startswith("manual"):
            should = False
        elif age_h is not None and age_h >= SPENDING_COOLDOWN_H and not still_unavail:
            # unknown reason, aged out
            should, why = True, f"aged_{age_h:.1f}h"

        if should and not (still_unavail and not force):
            if set_disabled(aid, False, ""):
                accounts.pop(aid, None)
                revived.append({"id": aid, "email": data.get("email"), "why": why})
        else:
            kept.append(
                {
                    "id": aid,
                    "email": data.get("email"),
                    "reason": reason,
                    "age_h": age_h,
                    "still_spending": still_spending,
                    "still_unavail": still_unavail,
                }
            )

    meta["last_revive"] = datetime.now().isoformat(timespec="seconds")
    save_quar_meta(meta)
    return {"revived": revived, "still_quarantined": kept}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarantine", action="store_true", help="disable spending-exhausted auths")
    ap.add_argument("--revive", action="store_true", help="re-enable after cooldown")
    ap.add_argument("--force-revive", action="store_true")
    ap.add_argument("--json", action="store_true", help="print full report json")
    args = ap.parse_args()

    sec = load_secrets()
    mgmt = sec.get("CPA_MGMT_KEY") or sec.get("MGMT_KEY")
    if not mgmt:
        print("missing CPA_MGMT_KEY", file=sys.stderr)
        return 2

    files = fetch_auth_files(mgmt)
    report = classify(files)

    actions = {}
    if args.quarantine:
        actions["quarantine"] = quarantine_spending(report)
        # re-scan not required for report of action
    if args.revive or args.force_revive:
        actions["revive"] = revive_if_due(report, force=args.force_revive)

    report["actions"] = actions
    STATE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        # compact human summary
        print(
            f"CPA号池扫描 total={report['total']} usable≈{report['usable']} "
            f"unavailable={report['unavailable']} "
            f"spending={report['by_tag'].get('spending_exhausted', 0)} "
            f"perm_denied={report['by_tag'].get('permission_denied', 0)}"
        )
        if report.get("spending_list"):
            print("额度耗尽(滚动24h自动恢复, 约200万token/窗):")
            for s in report["spending_list"]:
                tu = s.get("token_usage") or {}
                print(
                    f"  - {s.get('email')} "
                    f"tokens={tu.get('actual')}/{tu.get('limit')} "
                    f"ok={s.get('success')} fail={s.get('failed')}"
                )
        if report.get("unavailable_list"):
            print(f"不可用 permission-denied: {len(report['unavailable_list'])} 个（勿删，可能自行恢复）")
            for s in report["unavailable_list"][:8]:
                print(f"  - {s.get('email')} fail={s.get('failed')}")
            if len(report["unavailable_list"]) > 8:
                print(f"  ... +{len(report['unavailable_list'])-8}")
        if actions:
            print("actions:", json.dumps(actions, ensure_ascii=False))
        print(f"report: {STATE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
