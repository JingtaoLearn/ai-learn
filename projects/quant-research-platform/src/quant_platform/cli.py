from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import NoReturn, Sequence

import pandas as pd

from .datasets import publish_snapshot, snapshot_status
from .submissions import publish_submission, submission_status
from .updates import reconcile_daily_history


class CLIUsageError(ValueError):
    """Raised instead of argparse writing a non-JSON error."""


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CLIUsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = JSONArgumentParser(prog="research", exit_on_error=False)
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=JSONArgumentParser
    )

    data = commands.add_parser("data")
    data_commands = data.add_subparsers(
        dest="data_command", required=True, parser_class=JSONArgumentParser
    )
    snapshot = data_commands.add_parser("snapshot")
    snapshot.add_argument("--input", required=True)
    snapshot.add_argument("--root", required=True)
    snapshot.add_argument("--instrument", required=True)
    snapshot.add_argument("--provider", required=True)
    snapshot.add_argument("--market", required=True)
    snapshot.add_argument("--currency", required=True)
    snapshot.add_argument("--adjustment", required=True)
    update = data_commands.add_parser("update")
    update.add_argument("--input", required=True)
    update.add_argument("--expected-sessions", required=True)
    update.add_argument("--start", required=True)
    update.add_argument("--end", required=True)
    update.add_argument("--root", required=True)
    update.add_argument("--instrument", required=True)
    update.add_argument("--provider", required=True)
    update.add_argument("--market", required=True)
    update.add_argument("--currency", required=True)
    update.add_argument("--adjustment", required=True)
    status = data_commands.add_parser("status")
    status.add_argument("--root", required=True)
    status.add_argument("--instrument", required=True)

    submit = commands.add_parser("submit")
    submit.add_argument("--spec", required=True)
    submit.add_argument("--project-root", required=True)
    submit.add_argument("--root", required=True)

    submission = commands.add_parser("submission")
    submission_commands = submission.add_subparsers(
        dest="submission_command", required=True, parser_class=JSONArgumentParser
    )
    show = submission_commands.add_parser("show")
    show.add_argument("--root", required=True)
    show.add_argument("--submission-id", required=True)
    return parser


def _metadata(args: argparse.Namespace) -> dict[str, str]:
    return {
        "instrument": args.instrument,
        "provider": args.provider,
        "market": args.market,
        "currency": args.currency,
        "adjustment": args.adjustment,
    }


def _execute(args: argparse.Namespace) -> dict[str, str | int]:
    if args.command == "data" and args.data_command in {"snapshot", "update"}:
        frame = pd.read_csv(Path(args.input))
        metadata = _metadata(args)
        if args.data_command == "update":
            sessions = pd.read_csv(Path(args.expected_sessions))
            if len(sessions.columns) != 1:
                raise CLIUsageError(
                    "expected-sessions input must contain exactly one date column"
                )
            result = reconcile_daily_history(
                frame,
                sessions.iloc[:, 0],
                Path(args.root),
                metadata,
                args.start,
                args.end,
            )
            return {
                key: result[key]
                for key in ("status", "snapshot_id", "path", "update_id", "update_path")
            }
        return publish_snapshot(frame, Path(args.root), metadata)
    if args.command == "data" and args.data_command == "status":
        return snapshot_status(Path(args.root), args.instrument)
    if args.command == "submit":
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        return publish_submission(spec, Path(args.project_root), Path(args.root))
    if args.command == "submission" and args.submission_command == "show":
        return submission_status(Path(args.root), args.submission_id)
    raise CLIUsageError("unsupported command")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one platform command and emit exactly one JSON object."""

    try:
        try:
            args = _parser().parse_args(argv)
        except argparse.ArgumentError as exc:
            raise CLIUsageError(str(exc)) from exc
        result = _execute(args)
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except (CLIUsageError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
