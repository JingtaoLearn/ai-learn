from __future__ import annotations

import socket
import sys
import urllib.request

HTTP_ENDPOINTS = {
    "mlflow": "http://127.0.0.1:5000/health",
    "prefect": "http://127.0.0.1:4200/api/health",
}
failed = False

try:
    with socket.create_connection(("127.0.0.1", 8888), timeout=5):
        print("jupyter: reachable (authentication enabled)")
except OSError as exc:
    failed = True
    print(f"jupyter: FAILED: {exc}")

for name, url in HTTP_ENDPOINTS.items():
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            print(f"{name}: {response.status}")
    except Exception as exc:
        failed = True
        print(f"{name}: FAILED: {exc}")

sys.exit(1 if failed else 0)
