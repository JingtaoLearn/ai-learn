"""Send one message to an exact Hermes Agent Session."""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

SESSION_ID_RE = re.compile(r"(?m)^session_id:\s*(\S+)\s*$")
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class DeliveryRequest:
    target_profile: str
    target_session_id: str
    message: str
    workdir: Path


@dataclass(frozen=True)
class DeliveryResult:
    target_profile: str
    target_session_id: str
    response: str


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


def default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=build_delivery_environment(),
    )


def deliver_message(
    request: DeliveryRequest,
    *,
    runner: Runner | None = None,
) -> DeliveryResult:
    if not request.message:
        raise ValueError("message must not be empty")
    workdir = request.workdir.expanduser().resolve(strict=True)
    if not workdir.is_dir():
        raise ValueError(f"workdir is not a directory: {workdir}")

    real_home = Path(
        os.environ.get("HERMES_REAL_HOME") or pwd.getpwuid(os.getuid()).pw_dir
    )
    temporary_root = real_home / ".hermes" / "cache" / "session-send"
    temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_root.chmod(0o700)

    message_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="message-",
            suffix=".md",
            dir=temporary_root,
            delete=False,
        ) as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(request.message)
            handle.flush()
            os.fsync(handle.fileno())
            message_path = Path(handle.name)

        command = [
            "hermes",
            "-p",
            request.target_profile,
            "chat",
            "--resume",
            request.target_session_id,
            "--in",
            str(workdir),
            "-Q",
            "--query-file",
            str(message_path),
        ]
        selected_runner = runner or default_runner
        try:
            completed = selected_runner(command)
        except OSError as error:
            raise RuntimeError(f"delivery launch failed: {error}") from error

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"delivery exited {completed.returncode}{suffix}")

        match = SESSION_ID_RE.search(completed.stderr or "")
        observed_session = match.group(1) if match else None
        if observed_session != request.target_session_id:
            raise RuntimeError(
                "target session mismatch: "
                f"expected={request.target_session_id} observed={observed_session}"
            )

        return DeliveryResult(
            target_profile=request.target_profile,
            target_session_id=request.target_session_id,
            response=completed.stdout or "",
        )
    finally:
        if message_path is not None:
            message_path.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--profile", required=True, help="Target Hermes profile ID")
    value.add_argument("--session", required=True, help="Exact target Session ID")
    value.add_argument("--workdir", type=Path, required=True)
    source = value.add_mutually_exclusive_group(required=True)
    source.add_argument("--message")
    source.add_argument("--message-file", type=Path)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    message = (
        args.message
        if args.message is not None
        else args.message_file.expanduser().read_text(encoding="utf-8")
    )
    request = DeliveryRequest(
        target_profile=args.profile,
        target_session_id=args.session,
        message=message,
        workdir=args.workdir,
    )
    try:
        result = deliver_message(request)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "profile": result.target_profile,
                "session_id": result.target_session_id,
                "response": result.response,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
