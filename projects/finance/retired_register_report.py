#!/usr/bin/env python3
"""Fail-fast tombstone for the retired finance-specific registry."""

import sys

MESSAGE = """The finance-specific registry has been retired.
Publish the canonical HTML, then use:
  python3 /home/jingtao/ai-learn/projects/content-hub/register.py category --category-id investment-research
  python3 /home/jingtao/ai-learn/projects/content-hub/register.py item --card-json <item.json>
Existing finance report URLs remain read-only for compatibility.
"""

if __name__ == "__main__":
    print(MESSAGE, file=sys.stderr)
    raise SystemExit(2)
