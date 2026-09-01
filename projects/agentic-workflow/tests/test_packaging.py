from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import venv
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
PROFILE = json.loads((PROJECT_ROOT / "config" / "operating-profile.v1.json").read_text())


def test_installed_wheel_contains_migration_and_can_bootstrap_and_view(tmp_path: Path) -> None:
    source_directory = tmp_path / "source"
    shutil.copytree(PROJECT_ROOT / "src", source_directory / "src")
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", source_directory / "pyproject.toml")
    wheel_directory = tmp_path / "wheelhouse"
    wheel_directory.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--wheel-dir",
            str(wheel_directory),
            str(source_directory),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_directory.glob("agentic_workflow-*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]

    with zipfile.ZipFile(wheel) as archive:
        assert "agentic_workflow/migrations/0001_initial.sql" in archive.namelist()

    environment_directory = tmp_path / "installed"
    venv.EnvBuilder(with_pip=True).create(environment_directory)
    python = environment_directory / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "--no-index", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )

    profile_json = json.dumps(PROFILE, sort_keys=True)
    smoke_test = textwrap.dedent(
        f"""
        import json
        from pathlib import Path

        from agentic_workflow import UserDecision, WorkflowKernel

        payload = {{
            "project": {{"name": "Installed Wheel"}},
            "constitution": {{
                "user_sovereignty": True,
                "external_effects_require_authority": True,
            }},
            "goal": {{
                "outcome": "Prove the installed wheel boots",
                "scope": "ticket-211",
                "success_evidence": ["installed wheel bootstrap and view"],
                "constraints": ["no source-tree imports"],
                "accepted_tradeoffs": [],
                "non_goals": [],
            }},
            "operating_profile": json.loads({profile_json!r}),
        }}
        decision = UserDecision(
            project_id="wheel-project",
            source="packaging-test",
            source_event_id="bootstrap-1",
            authenticated_actor="wheel-user",
            scope="PROJECT_INTENT",
            verbatim_text="Bootstrap from the installed wheel.",
            nonce="wheel-nonce-1",
            replay_identity="wheel-bootstrap-1",
            provenance={{"channel": "packaging-test"}},
            decision_kind="BOOTSTRAP_PROJECT",
            complete_revision_payload=payload,
        )

        class ExactAuthenticator:
            def authenticate(self, candidate: UserDecision) -> bool:
                return candidate == decision

        kernel = WorkflowKernel(
            Path("installed-wheel.sqlite3"),
            decision_authenticator=ExactAuthenticator(),
        )
        receipt = kernel.record(decision)
        view = kernel.view("wheel-project")
        assert receipt.outcome == "PROJECT_BOOTSTRAPPED"
        assert view.current_goal == payload["goal"]
        assert view.daily_brief == {{"status": "INITIAL", "material_changes": []}}
        print("installed-wheel-bootstrap-view-ok")
        """
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(python), "-I", "-c", smoke_test],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "installed-wheel-bootstrap-view-ok"
