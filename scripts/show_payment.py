"""Печать платежа из stdin для demo.sh."""

import json
import sys

p = json.load(sys.stdin)
if "status" not in p:
    print("   ", p)
    sys.exit()
line = f"   => payment {p['status']}"
if p["failure_code"]:
    line += f" ({p['failure_code']})"
if url := (p.get("next_action") or {}).get("url"):
    line += f"  next_action: {url}"
print(line)
for a in p["attempts"]:
    extra = f"{a['decline_code']}/{a['decline_class']}" if a["decline_code"] else ""
    dup = " DUPLICATE" if a["is_duplicate"] else ""
    print(f"      #{a['seq']} {a['provider']:<11} {a['mode']:<6} {a['status']:<15} {extra}{dup}")
