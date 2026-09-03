"""Durably deliver a terminal Agent event to one persistent Owner Bot Chat."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

TERMINAL_EVENT_TYPES = frozenset({"RESULT_READY", "BLOCKED", "FAILED"})
SESSION_ID_RE = re.compile(r"(?m)^session_id:\s*(\S+)\s*$")
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class EventRequest:
    product_id: str
    event_type: str
    source_profile: str
    owner_profile: str
    owner_session_id: str
    run_id: str
    action_id: str
    artifact_path: Path
    summary: str
    product_workspace: Path | None = None


@dataclass(frozen=True)
class EmitResult:
    event_id: str
    event_dir: Path
    status: str
    owner_session_id: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def build_event(request: EventRequest) -> tuple[str, dict[str, object]]:
    if request.event_type not in TERMINAL_EVENT_TYPES:
        allowed = ", ".join(sorted(TERMINAL_EVENT_TYPES))
        raise ValueError(
            f"unsupported terminal event type {request.event_type!r}; expected one of {allowed}"
        )
    artifact = request.artifact_path.expanduser().resolve(strict=True)
    if not artifact.is_file():
        raise ValueError(f"artifact is not a regular file: {artifact}")
    artifact_sha256 = sha256_file(artifact)
    identity = {
        "schema_version": 1,
        "product_id": request.product_id,
        "event_type": request.event_type,
        "source_profile": request.source_profile,
        "owner_profile": request.owner_profile,
        "owner_session_id": request.owner_session_id,
        "run_id": request.run_id,
        "action_id": request.action_id,
        "artifact_path": str(artifact),
        "artifact_sha256": artifact_sha256,
        "summary": request.summary,
    }
    event_id = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    event = {
        **identity,
        "event_id": event_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return event_id, event


def render_prompt(event: dict[str, object]) -> str:
    return f"""[Agentic Workflow terminal event]
event_id: {event['event_id']}
product_id: {event['product_id']}
event_type: {event['event_type']}
source_profile: {event['source_profile']}
owner_session_id: {event['owner_session_id']}
run_id: {event['run_id']}
action_id: {event['action_id']}
artifact_path: {event['artifact_path']}
artifact_sha256: {event['artifact_sha256']}
summary: {event['summary']}

This durable terminal event is addressed to the canonical Product Owner Session named above. Verify the artifact hash, read the artifact, and decide the next bounded Action from the product Goal and Principles. Process this event idempotently: if event_id was already absorbed, report that fact without repeating side effects. Do not trust this summary instead of the artifact.
"""


def build_delivery_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(source or os.environ)
    environment.pop("HERMES_HOME", None)
    real_home = environment.get("HERMES_REAL_HOME") or pwd.getpwuid(os.getuid()).pw_dir
    environment["HOME"] = real_home
    return environment


def default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=build_delivery_environment(),
    )


def emit_event(
    request: EventRequest,
    state_root: Path,
    *,
    runner: Runner = default_runner,
) -> EmitResult:
    event_id, event = build_event(request)
    event_dir = state_root.expanduser().resolve() / event_id
    event_dir.mkdir(parents=True, exist_ok=True)
    lock_path = event_dir / ".lock"

    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        event_path = event_dir / "event.json"
        if not event_path.exists():
            atomic_json(event_path, event)
            (event_dir / "prompt.md").write_text(render_prompt(event), encoding="utf-8")

        delivered_path = event_dir / "delivered.json"
        if delivered_path.exists():
            delivered = json.loads(delivered_path.read_text(encoding="utf-8"))
            if delivered.get("owner_session_id") == request.owner_session_id:
                return EmitResult(
                    event_id=event_id,
                    event_dir=event_dir,
                    status="deduplicated",
                    owner_session_id=request.owner_session_id,
                )

        attempts_dir = event_dir / "attempts"
        attempts_dir.mkdir(exist_ok=True)
        attempt_number = len(list(attempts_dir.glob("*.json"))) + 1
        workspace = (request.product_workspace or request.artifact_path.parent).expanduser().resolve()
        command = [
            "hermes",
            "-p",
            request.owner_profile,
            "chat",
            "--in",
            str(workspace),
            "-c",
            "Bot Chat",
            "--create-if-missing",
            "-Q",
            "--query-file",
            str(event_dir / "prompt.md"),
        ]
        completed = runner(command)
        session_metadata = "\n".join(
            part for part in (completed.stdout or "", completed.stderr or "") if part
        )
        match = SESSION_ID_RE.search(session_metadata)
        observed_session = match.group(1) if match else None
        failure: str | None = None
        if completed.returncode != 0:
            failure = f"owner delivery exited {completed.returncode}"
        elif observed_session != request.owner_session_id:
            failure = (
                "owner session mismatch: "
                f"expected={request.owner_session_id} observed={observed_session}"
            )

        response = completed.stdout or ""
        attempt = {
            "attempt": attempt_number,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "returncode": completed.returncode,
            "observed_owner_session_id": observed_session,
            "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
            "stderr": completed.stderr or "",
            "status": "failed" if failure else "delivered",
            "failure": failure,
        }
        atomic_json(attempts_dir / f"{attempt_number:04d}.json", attempt)
        (attempts_dir / f"{attempt_number:04d}.response.txt").write_text(
            response, encoding="utf-8"
        )

        if failure:
            raise RuntimeError(failure)

        atomic_json(
            delivered_path,
            {
                "event_id": event_id,
                "delivered_at": datetime.now(timezone.utc).isoformat(),
                "owner_session_id": observed_session,
                "attempt": attempt_number,
                "response_sha256": attempt["response_sha256"],
            },
        )
        return EmitResult(
            event_id=event_id,
            event_dir=event_dir,
            status="delivered",
            owner_session_id=request.owner_session_id,
        )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--state-root", type=Path, required=True)
    value.add_argument("--product-workspace", type=Path, required=True)
    value.add_argument("--product-id", required=True)
    value.add_argument("--event-type", choices=sorted(TERMINAL_EVENT_TYPES), required=True)
    value.add_argument("--source-profile", required=True)
    value.add_argument("--owner-profile", required=True)
    value.add_argument("--owner-session-id", required=True)
    value.add_argument("--run-id", required=True)
    value.add_argument("--action-id", required=True)
    value.add_argument("--artifact", type=Path, required=True)
    value.add_argument("--summary", required=True)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    request = EventRequest(
        product_id=args.product_id,
        event_type=args.event_type,
        source_profile=args.source_profile,
        owner_profile=args.owner_profile,
        owner_session_id=args.owner_session_id,
        run_id=args.run_id,
        action_id=args.action_id,
        artifact_path=args.artifact,
        summary=args.summary,
        product_workspace=args.product_workspace,
    )
    try:
        result = emit_event(request, args.state_root)
    except (OSError, ValueError, RuntimeError) as error:
        print(canonical_json({"ok": False, "error": str(error)}))
        return 1
    print(canonical_json({"ok": True, **asdict(result), "event_dir": str(result.event_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
