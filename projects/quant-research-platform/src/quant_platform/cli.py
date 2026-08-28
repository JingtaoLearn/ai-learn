from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import NoReturn, Sequence

import pandas as pd

from .datasets import publish_snapshot, snapshot_status
from .catalog import initialize_catalog
from .dataset_service import DatasetService
from .experiment_service import ExperimentService
from .operator_service import OperatorService
from .parameter_study import ParameterStudy, StudyNotFoundError
from .resolved_runner import effective_execution_identity
from .runner import run_submission
from .strategy_runner import run_strategy_config
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

    run = commands.add_parser("run")
    run.add_argument("--root", required=True)
    run.add_argument("--submission-id", required=True)
    run.add_argument("--attempt-id", required=True)
    run.add_argument("--timeout-seconds", required=True, type=float)

    strategy = commands.add_parser("strategy")
    strategy_commands = strategy.add_subparsers(
        dest="strategy_command", required=True, parser_class=JSONArgumentParser
    )
    strategy_run = strategy_commands.add_parser("run")
    strategy_run.add_argument("--config", required=True)
    strategy_run.add_argument("--project-root")

    operator = commands.add_parser("operator")
    operator_commands = operator.add_subparsers(
        dest="operator_command", required=True, parser_class=JSONArgumentParser
    )
    operator_list = operator_commands.add_parser("list")
    operator_list.add_argument("--root", required=True)
    operator_detail = operator_commands.add_parser("detail")
    operator_detail.add_argument("--root", required=True)
    operator_detail.add_argument("--operator-id", required=True)
    operator_detail.add_argument("--version")
    operator_submit = operator_commands.add_parser("submit")
    operator_submit.add_argument("--root", required=True)
    operator_submit.add_argument("--spec", required=True)
    operator_submit.add_argument("--runner-image", required=True)

    template = commands.add_parser("template")
    template_commands = template.add_subparsers(
        dest="template_command", required=True, parser_class=JSONArgumentParser
    )
    template_detail = template_commands.add_parser("detail")
    template_detail.add_argument("--root", required=True)
    template_detail.add_argument("--name", required=True)
    template_detail.add_argument("--version", required=True)

    task = commands.add_parser("task")
    task_commands = task.add_subparsers(
        dest="task_command", required=True, parser_class=JSONArgumentParser
    )
    for name in ("resolve", "submit"):
        task_parser = task_commands.add_parser(name)
        task_parser.add_argument("--root", required=True)
        task_parser.add_argument("--spec", required=True)
        if name == "submit":
            task_parser.add_argument("--action-id", required=True)
    task_rerun = task_commands.add_parser("rerun")
    task_rerun.add_argument("--root", required=True)
    task_rerun.add_argument("--experiment-id", required=True)
    task_rerun.add_argument("--action-id", required=True)

    experiment = commands.add_parser("experiment")
    experiment_commands = experiment.add_subparsers(
        dest="experiment_command", required=True, parser_class=JSONArgumentParser
    )
    experiment_list = experiment_commands.add_parser("list")
    experiment_list.add_argument("--root", required=True)
    experiment_detail = experiment_commands.add_parser("detail")
    experiment_detail.add_argument("--root", required=True)
    experiment_detail.add_argument("--experiment-id", required=True)

    attempt = commands.add_parser("attempt")
    attempt_commands = attempt.add_subparsers(
        dest="attempt_command", required=True, parser_class=JSONArgumentParser
    )
    attempt_list = attempt_commands.add_parser("list")
    attempt_list.add_argument("--root", required=True)
    attempt_list.add_argument("--experiment-id", required=True)
    attempt_detail = attempt_commands.add_parser("detail")
    attempt_detail.add_argument("--root", required=True)
    attempt_detail.add_argument("--attempt-id", required=True)
    attempt_recover = attempt_commands.add_parser("recover")
    attempt_recover.add_argument("--root", required=True)
    attempt_recover.add_argument("--attempt-id", required=True)
    attempt_recover.add_argument("--action-id", required=True)

    study = commands.add_parser("study")
    study_commands = study.add_subparsers(
        dest="study_command", required=True, parser_class=JSONArgumentParser
    )
    study_preview = study_commands.add_parser("preview")
    study_preview.add_argument("--root", required=True)
    study_preview.add_argument("--spec", required=True)
    study_submit = study_commands.add_parser("submit")
    study_submit.add_argument("--root", required=True)
    study_submit.add_argument("--spec", required=True)
    study_submit.add_argument("--expected-preview-digest", required=True)
    study_submit.add_argument("--action-id", required=True)
    study_list = study_commands.add_parser("list")
    study_list.add_argument("--root", required=True)
    for name in ("advance", "detail"):
        study_parser = study_commands.add_parser(name)
        study_parser.add_argument("--root", required=True)
        study_parser.add_argument("--study-id", required=True)
    study_control = study_commands.add_parser("control")
    study_control.add_argument("--root", required=True)
    study_control.add_argument("--study-id", required=True)
    study_control.add_argument("--operation", required=True)
    study_control.add_argument("--action-id", required=True)
    return parser


def _metadata(args: argparse.Namespace) -> dict[str, str]:
    return {
        "instrument": args.instrument,
        "provider": args.provider,
        "market": args.market,
        "currency": args.currency,
        "adjustment": args.adjustment,
    }


def _strict_json_file(path: str) -> object:
    return json.loads(
        Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=lambda pairs: _unique_pairs(pairs),
        parse_constant=lambda constant: (_ for _ in ()).throw(
            CLIUsageError(f"non-finite JSON number: {constant}")
        ),
    )


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise CLIUsageError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _domain_services(root: str) -> tuple:
    catalog = initialize_catalog(Path(root))
    datasets = DatasetService(catalog)
    experiments = ExperimentService(
        catalog,
        execution_identity=effective_execution_identity(
            None, os.environ.get("QUANT_RUNNER_IMAGE")
        ),
        datasets=datasets,
    )
    return catalog, experiments


def _study_service(root: str) -> ParameterStudy:
    catalog, experiments = _domain_services(root)
    return ParameterStudy.from_experiments(
        catalog,
        experiments=experiments,
        release_locator=(
            os.environ.get("QUANT_RELEASE_LOCATOR")
            or str(Path(root).resolve())
        ),
    )


def _advance_study_until_blocked(
    studies: ParameterStudy,
    study_id: str,
) -> dict:
    progress_statuses = {
        "ADVANCED",
        "METRIC_DOCUMENT_VERIFIED",
        "OUTER_SELECTION_RECORDED",
        "CHAMPION_FROZEN",
        "HOLDOUT_CLAIMED",
    }
    for _ in range(1024):
        result = studies.advance(study_id)
        if result.get("status") not in progress_statuses:
            return result
    raise RuntimeError("bounded Study advance exceeded 1024 internal transitions")


def _execute(args: argparse.Namespace) -> dict:
    if args.command == "data" and args.data_command in {"snapshot", "update"}:
        frame = pd.read_csv(Path(args.input))
        metadata = _metadata(args)
        if args.data_command == "update":
            sessions = pd.read_csv(Path(args.expected_sessions))
            if list(sessions.columns) != ["Date"]:
                raise CLIUsageError(
                    "expected-sessions input schema must be exactly one column named Date"
                )
            result = reconcile_daily_history(
                frame,
                sessions["Date"],
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
    if args.command == "run":
        result = run_submission(
            Path(args.root),
            args.submission_id,
            args.attempt_id,
            args.timeout_seconds,
        )
        return {
            key: result[key] for key in ("attempt_id", "run_id", "outcome", "path")
        }
    if args.command == "strategy" and args.strategy_command == "run":
        result = (
            run_strategy_config(
                Path(args.config),
                project_root=Path(args.project_root),
            )
            if args.project_root is not None
            else run_strategy_config(Path(args.config))
        )
        return {
            key: result[key]
            for key in (
                "status",
                "run_id",
                "path",
                "config_sha256",
                "dataset_snapshot_id",
            )
        }
    if args.command == "operator":
        catalog, _ = _domain_services(args.root)
        operators = OperatorService(
            catalog,
            runner_image=getattr(args, "runner_image", None),
        )
        if args.operator_command == "list":
            return {"operators": operators.list()}
        if args.operator_command == "detail":
            return {
                "operator": operators.detail(args.operator_id, args.version),
                "versions": operators.list_versions(args.operator_id),
            }
        if args.operator_command == "submit":
            return operators.submit(_strict_json_file(args.spec))
    if args.command == "template" and args.template_command == "detail":
        catalog, _ = _domain_services(args.root)
        return {"template": catalog.template_detail(args.name, args.version)}
    if args.command == "task":
        _, experiments = _domain_services(args.root)
        if args.task_command == "resolve":
            return {"resolved": experiments.resolve_task(_strict_json_file(args.spec))}
        if args.task_command == "submit":
            return experiments.submit(
                _strict_json_file(args.spec), action_id=args.action_id
            )
        if args.task_command == "rerun":
            return experiments.rerun(
                args.experiment_id, action_id=args.action_id
            )
    if args.command == "experiment":
        _, experiments = _domain_services(args.root)
        if args.experiment_command == "list":
            return {"experiments": experiments.list_experiments()}
        if args.experiment_command == "detail":
            return {"experiment": experiments.experiment_detail(args.experiment_id)}
    if args.command == "attempt":
        _, experiments = _domain_services(args.root)
        if args.attempt_command == "list":
            return {"attempts": experiments.list_attempts(args.experiment_id)}
        if args.attempt_command == "detail":
            return {"attempt": experiments.attempt_detail(args.attempt_id)}
        if args.attempt_command == "recover":
            return experiments.create_replacement_attempt(
                args.attempt_id, action_id=args.action_id
            )
    if args.command == "study":
        studies = _study_service(args.root)
        if args.study_command == "preview":
            return studies.preview(_strict_json_file(args.spec))
        if args.study_command == "submit":
            return studies.submit(
                _strict_json_file(args.spec),
                expected_preview_digest=args.expected_preview_digest,
                action_id=args.action_id,
            )
        if args.study_command == "list":
            return {"studies": studies.list()}
        if args.study_command == "detail":
            return {"study": studies.detail(args.study_id)}
        if args.study_command == "advance":
            return _advance_study_until_blocked(studies, args.study_id)
        if args.study_command == "control":
            return studies.control(
                args.study_id,
                args.operation,
                action_id=args.action_id,
            )
    raise CLIUsageError("unsupported command")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one platform command and emit exactly one JSON object."""

    try:
        try:
            args = _parser().parse_args(argv)
        except argparse.ArgumentError as exc:
            raise CLIUsageError(str(exc)) from exc
        result = _execute(args)
        print(json.dumps({"ok": True, **result}, sort_keys=True, allow_nan=False))
        return 0
    except (
        CLIUsageError,
        OSError,
        ValueError,
        RuntimeError,
        StudyNotFoundError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
                allow_nan=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
