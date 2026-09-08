#!/usr/bin/env python3
"""Executable ailearn wrapper for the reviewed Gold production API job."""

import importlib
import os
import sys
import types
from pathlib import Path


if len(sys.argv) != 1:
    raise SystemExit("gold production API action accepts no arguments")
hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).absolute()
package_root = hermes_home / "lib" / "quantresearch-production-client" / "quant_platform"
package = types.ModuleType("quant_platform")
package.__path__ = [str(package_root)]
package.__package__ = "quant_platform"
sys.modules["quant_platform"] = package
client = importlib.import_module("quant_platform.production_schedule_client")
raise SystemExit(client.main(["--job-id", "1cd5557264db"]))
