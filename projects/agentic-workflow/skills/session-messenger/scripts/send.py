#!/usr/bin/env python3
"""Dispatch one addressed message to an existing Hermes Agent Session."""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SESSION_RE = re.compile(r"(?m)^session_id:\s*(\S+)\s*$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]+$")
RESULT_TTL_SECONDS = 24 * 60 * 60


def real_home() -> Path:
    return Path(os.environ.get("HERMES_REAL_HOME") or pwd.getpwuid(os.getuid()).pw_dir)


def cache_dir() -> Path:
    path = real_home() / ".hermes" / "cache" / "session-messenger"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def clean_old_results(root: Path) -> None:
    cutoff = time.time() - RESULT_TTL_SECONDS
    for path in root.glob("result-*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def checked_id(label: str, value: str) -> str:
    value = value.strip()
    if not value or not SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def atomic_json(path: Path, payload: dict) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        tmp.unlink(missing_ok=True)


def target_environment() -> dict[str, str]:
    allowed = {"LANG", "LOGNAME", "PATH", "SHELL", "TERM", "TZ", "USER", "XDG_RUNTIME_DIR"}
    env = {key: value for key, value in os.environ.items() if key in allowed or key.startswith("LC_")}
    home = str(real_home())
    env["HOME"] = home
    env["HERMES_REAL_HOME"] = home
    return env


def envelope(args: argparse.Namespace, message_id: str, body: str) -> str:
    lines = [
        "[SESSION-MESSAGE v1]",
        f"message_id: {message_id}",
        f"kind: {args.kind}",
        f"to_profile: {args.to_profile}",
        f"to_session: {args.to_session}",
    ]
    if args.from_profile:
        lines.extend(
            [
                f"from_profile: {args.from_profile}",
                f"from_session: {args.from_session}",
                "reply_available: true",
                "reply_rule: Use the session-messenger skill, swap the from/to addresses, and preserve message_id as correlation_id.",
            ]
        )
    else:
        lines.extend([f"source: {args.source}", "reply_available: false"])
    if args.correlation_id:
        lines.append(f"correlation_id: {args.correlation_id}")
    lines.extend(["", body.strip(), ""])
    return "\n".join(lines)


def delivery_result(job: dict) -> dict:
    message_path = Path(job["message_path"])
    command = [
        "hermes",
        "-p",
        job["to_profile"],
        "chat",
        "--resume",
        job["to_session"],
        "--in",
        job["workdir"],
        "--pass-session-id",
        "-Q",
        "--query-file",
        str(message_path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, env=target_environment())
        match = SESSION_RE.search(completed.stderr or "")
        observed = match.group(1) if match else None
        ok = completed.returncode == 0 and observed == job["to_session"]
        return {
            "ok": ok,
            "message_id": job["message_id"],
            "to_profile": job["to_profile"],
            "to_session": job["to_session"],
            "observed_session": observed,
            "exit_code": completed.returncode,
            **({"error": (completed.stderr or completed.stdout or "delivery failed").strip()[-2000:]} if not ok else {}),
        }
    except Exception as error:
        return {
            "ok": False,
            "message_id": job["message_id"],
            "to_profile": job["to_profile"],
            "to_session": job["to_session"],
            "error": f"{type(error).__name__}: {error}",
        }


def run_job(job_path: Path) -> int:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    result_path = Path(job["result_path"])
    message_path = Path(job["message_path"])
    try:
        result = delivery_result(job)
        atomic_json(result_path, result)
        return 0 if result["ok"] else 1
    finally:
        message_path.unlink(missing_ok=True)
        job_path.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--to-profile")
    value.add_argument("--to-session")
    value.add_argument("--from-profile")
    value.add_argument("--from-session")
    value.add_argument("--source")
    value.add_argument("--kind", default="MESSAGE")
    value.add_argument("--correlation-id")
    value.add_argument("--workdir", type=Path, default=Path.cwd())
    body = value.add_mutually_exclusive_group()
    body.add_argument("--message")
    body.add_argument("--message-file", type=Path)
    value.add_argument("--deliver-job", type=Path, help=argparse.SUPPRESS)
    return value


def main() -> int:
    args = parser().parse_args()
    if args.deliver_job:
        return run_job(args.deliver_job)

    try:
        args.to_profile = checked_id("to-profile", args.to_profile or "")
        args.to_session = checked_id("to-session", args.to_session or "")
        args.kind = checked_id("kind", args.kind)
        if bool(args.from_profile) != bool(args.from_session):
            raise ValueError("from-profile and from-session must be supplied together")
        if args.from_profile:
            args.from_profile = checked_id("from-profile", args.from_profile)
            args.from_session = checked_id("from-session", args.from_session)
            if args.source:
                raise ValueError("source cannot be combined with a replyable Session sender")
        else:
            args.source = checked_id("source", args.source or "")
        if args.correlation_id:
            args.correlation_id = checked_id("correlation-id", args.correlation_id)
        body = args.message if args.message is not None else (
            args.message_file.expanduser().read_text(encoding="utf-8") if args.message_file else ""
        )
        if not body.strip():
            raise ValueError("message must not be empty")
        workdir = args.workdir.expanduser().resolve(strict=True)
        if not workdir.is_dir():
            raise ValueError(f"workdir is not a directory: {workdir}")

        root = cache_dir()
        clean_old_results(root)
        message_id = uuid.uuid4().hex
        message_path = root / f"message-{message_id}.md"
        job_path = root / f"job-{message_id}.json"
        result_path = root / f"result-{message_id}.json"
        message_path.write_text(envelope(args, message_id, body), encoding="utf-8")
        message_path.chmod(0o600)
        atomic_json(
            job_path,
            {
                "message_id": message_id,
                "to_profile": args.to_profile,
                "to_session": args.to_session,
                "workdir": str(workdir),
                "message_path": str(message_path),
                "result_path": str(result_path),
            },
        )
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--deliver-job", str(job_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=target_environment(),
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "dispatched",
                    "message_id": message_id,
                    "to_profile": args.to_profile,
                    "to_session": args.to_session,
                    "replyable": bool(args.from_profile),
                    "result_file": str(result_path),
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
