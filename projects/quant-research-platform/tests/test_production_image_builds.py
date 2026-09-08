from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest


PROJECT = Path(__file__).parents[1]
NGINX_INDEX = (
    "nginx:1.27.5-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
)
NGINX_AMD64 = "sha256:62223d644fa234c3a1cc785ee14242ec47a77364226f1c811d2f669f96dc2ac8"
pytestmark = pytest.mark.skipif(
    os.environ.get("QR_RUN_PRODUCTION_IMAGE_BUILDS") != "1",
    reason="formal production image builds run only on the assigned flearn evidence host",
)


def command(*argv: str) -> str:
    completed = subprocess.run(
        argv,
        cwd=PROJECT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def image_config(image: str) -> dict:
    return json.loads(command("docker", "image", "inspect", "--format", "{{json .Config}}", image))


def test_both_production_images_build_and_pinned_report_edge_is_amd64_compatible() -> None:
    suffix = uuid.uuid4().hex
    api = f"quantresearch-production-api-test:{suffix}"
    proxy = f"quantresearch-provider-proxy-test:{suffix}"
    try:
        command(
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--file",
            "production/Dockerfile.api",
            "--tag",
            api,
            ".",
        )
        command(
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--file",
            "production/Dockerfile.provider-proxy",
            "--tag",
            proxy,
            ".",
        )

        api_config = image_config(api)
        assert api_config["User"] == "10001:10001"
        assert api_config["Entrypoint"] == ["python", "-m", "quant_platform.production_web"]

        proxy_config = image_config(proxy)
        assert proxy_config["User"] == "10201:10201"
        assert proxy_config["Entrypoint"] == [
            "/usr/local/bin/quantresearch-egress-proxy",
            "--config",
            "/etc/quantresearch-egress-proxy/config.json",
        ]
        assert command(
            "docker", "run", "--rm", "--entrypoint", "getent", proxy, "group", "proxy"
        ).startswith("proxy:")
        assert command(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "getent",
            proxy,
            "passwd",
            "quantresearch-proxy",
        ).split(":")[2:4] == ["10201", "10201"]

        image_ids = {
            "production_api": command("docker", "image", "inspect", "--format", "{{.Id}}", api),
            "provider_proxy": command(
                "docker", "image", "inspect", "--format", "{{.Id}}", proxy
            ),
        }

        raw = json.loads(command("docker", "buildx", "imagetools", "inspect", "--raw", NGINX_INDEX))
        matching = [
            item
            for item in raw.get("manifests", [])
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == "amd64"
        ]
        assert [item["digest"] for item in matching] == [NGINX_AMD64]
        command(
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--user",
            "10101:10101",
            "--entrypoint",
            "nginx",
            NGINX_INDEX,
            "-v",
        )
        print(json.dumps({"built_images": image_ids, "report_edge_amd64": NGINX_AMD64}, sort_keys=True))
    finally:
        subprocess.run(
            ["docker", "image", "rm", "--force", api, proxy],
            cwd=PROJECT,
            check=False,
            capture_output=True,
            text=True,
        )
