#!/usr/bin/env python3
"""Executable ailearn wrapper for one reviewed Gold FOCuS calibration."""

import hashlib
import importlib
import os
import sys
import types
from pathlib import Path


if len(sys.argv) != 1:
    raise SystemExit("FOCuS calibration production API action accepts no arguments")
hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).absolute()
client_root = hermes_home / "lib" / "quantresearch-production-client"
package_root = client_root / "quant_platform"
package = types.ModuleType("quant_platform")
package.__path__ = [str(package_root)]
package.__package__ = "quant_platform"
sys.modules["quant_platform"] = package
client_module = importlib.import_module("quant_platform.production_client")
contract_module = importlib.import_module("quant_platform.production_contract")

authority_path = client_root / "focus-calibration-authority.json"
authority_sha256 = hashlib.sha256(authority_path.read_bytes()).hexdigest()
request = contract_module.ProductionRequest.build_operation(
    job_id=contract_module.FOCUS_CALIBRATION_JOB_ID,
    operation=contract_module.FOCUS_CALIBRATION_OPERATION,
    production_manifest_sha256=authority_sha256,
)
secrets = hermes_home / "secrets" / "quantresearch-production"
tls = client_module.ClientTLS(
    base_url="https://127.0.0.1:8443",
    client_certificate=secrets / "client.crt",
    client_private_key=secrets / "client.key",
    server_ca=secrets / "server-ca.crt",
    timeout_seconds=60.0,
)
client = client_module.ProductionClient(
    client_module.StdlibMTLSTransport(tls), poll_attempts=360
)
manifest = client.submit_and_wait(request)
calibration = client.verify_focus_calibration(manifest)
notification = (
    "Gold FOCuS calibration sealed"
    f" · result {manifest['result_id']}"
    f" · calibration {manifest['calibration_sha256']}"
    f" · status {calibration['status']}"
)
sys.stdout.write(notification)
