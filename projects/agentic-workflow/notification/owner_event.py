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
import uuid
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
    atomic_text(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)
    path.chmod(0o600)
    fsync_directory(path.parent)


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
    source_environment = dict(source or os.environ)
    real_home = source_environment.get("HERMES_REAL_HOME") or pwd.getpwuid(os.getuid()).pw_dir
    allowed = {
        "LANG",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TERM",
        "TZ",
        "USER",
        "XDG_RUNTIME_DIR",
    }
    environment = {
        key: value
        for key, value in source_environment.items()
        if key in allowed or key.startswith("LC_")
    }
    environment["HOME"] = real_home
    environment["HERMES_REAL_HOME"] = real_home
    return environment


def initialize_event_directory(
    state_root: Path,
    event_dir: Path,
    event: dict[str, object],
) -> None:
    if event_dir.exists():
        event_dir.chmod(0o700)
        event_path = event_dir / "event.json"
        prompt_path = event_dir / "prompt.md"
        if not event_path.is_file() or not prompt_path.is_file():
            raise RuntimeError(f"incomplete event initialization: {event_dir}")
        event_path.chmod(0o600)
        prompt_path.chmod(0o600)
        return

    temporary = state_root / f".{event['event_id']}.{uuid.uuid4().hex}.tmp"
    secure_directory(temporary)
    try:
        atomic_json(temporary / "event.json", event)
        atomic_text(temporary / "prompt.md", render_prompt(event))
        fsync_directory(temporary)
        os.replace(temporary, event_dir)
        fsync_directory(state_root)
    finally:
        if temporary.exists():
            for child in temporary.iterdir():
                child.unlink()
            temporary.rmdir()


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
    state_root = state_root.expanduser().resolve()
    secure_directory(state_root)
    locks_dir = state_root / ".locks"
    secure_directory(locks_dir)
    event_dir = state_root / event_id
    lock_path = locks_dir / f"{event_id}.lock"

    with lock_path.open("a+", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        initialize_event_directory(state_root, event_dir, event)

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
        secure_directory(attempts_dir)
        attempt_number = len(list(attempts_dir.glob("*.json"))) + 1
        workspace = (request.product_workspace or request.artifact_path.parent).expanduser().resolve()
        command = [
            "hermes",
            "-p",
            request.owner_profile,
            "chat",
            "--resume",
            request.owner_session_id,
            "--in",
            str(workspace),
            "-Q",
            "--query-file",
            str(event_dir / "prompt.md"),
        ]
        launch_failure: str | None = None
        try:
            completed = runner(command)
        except OSError as error:
            completed = subprocess.CompletedProcess(
                args=command,
                returncode=-1,
                stdout="",
                stderr=str(error),
            )
            launch_failure = f"owner delivery launch failed: {error}"
        match = SESSION_ID_RE.search(completed.stderr or "")
        observed_session = match.group(1) if match else None
        failure: str | None = launch_failure
        if failure is None and completed.returncode != 0:
            failure = f"owner delivery exited {completed.returncode}"
        elif failure is None and observed_session != request.owner_session_id:
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
        atomic_text(attempts_dir / f"{attempt_number:04d}.response.txt", response)

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
