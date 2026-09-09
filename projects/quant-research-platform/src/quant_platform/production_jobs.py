from __future__ import annotations

import hashlib
import html
import json
import math
import os
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .production_contract import SHA256, canonical_json_bytes


class ProductionJobError(RuntimeError):
    """Raised when immutable job input cannot produce a verified action."""


@dataclass(frozen=True)
class CanonicalJsonBytes:
    """Validated canonical JSON bytes for the already-encoded identity seam."""

    value: bytes

    def __post_init__(self) -> None:
        if type(self.value) is not bytes:
            raise TypeError("canonical JSON identity input must contain bytes")
        try:
            decoded = json.loads(self.value)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionJobError("canonical JSON identity input is invalid") from exc
        if canonical_json_bytes(decoded) != self.value:
            raise ProductionJobError("canonical JSON identity input is not canonical")


class ProviderClient(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], maximum_bytes: int) -> bytes: ...


@dataclass(frozen=True)
class TrendConfig:
    window_sessions: int
    ema_span: int
    buy_threshold_pct_per_day: float
    sell_threshold_pct_per_day: float
    anchor_date: date


@dataclass(frozen=True)
class JobComputation:
    job_id: str
    model_id: str
    production_manifest_sha256: str
    report_uuid: str
    provider_url: str
    raw_name: str
    raw_bytes: bytes
    normalized_bytes: bytes
    action: dict[str, Any]
    report_html: bytes
    notification_bytes: bytes
    experiment_id: str
    attempt_id: str


@dataclass(frozen=True)
class ProductionInput:
    kind: str
    identity: Mapping[str, Any]
    payload: bytes

    def __post_init__(self) -> None:
        if self.kind not in {"provider-get", "no-network-operation"}:
            raise ProductionJobError("production input kind is not supported")
        if type(self.payload) is not bytes:
            raise ProductionJobError("production input payload must be bytes")


@dataclass(frozen=True)
class FormalComputation:
    job_id: str
    production_manifest_sha256: str
    operation: str
    authority_sha256: str
    files: Mapping[str, bytes]
    experiment_id: str
    attempt_id: str


ProductionComputation = JobComputation | FormalComputation


_INPUT_MEMBERS = frozenset({"identity.json", "raw.bin"})
_DAILY_COMPUTATION_MEMBERS = frozenset(
    {
        "identity.json",
        "raw.bin",
        "normalized.json",
        "action.json",
        "report.html",
        "notification.txt",
    }
)
_FORMAL_RESULT_MEMBERS = frozenset(
    {
        "calibration.json",
        "03-CALIBRATION_CLAIMED.json",
        "04-CALIBRATION_SEALED.json",
    }
)
_FORMAL_COMPUTATION_MEMBERS = frozenset({"identity.json", *_FORMAL_RESULT_MEMBERS})
_STAGED_PACKAGE_IDENTITY_DOMAIN = b"quantresearch-production-staged-package/v1\0"


def _stat_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _safe_member_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and name not in {"", ".", ".."}
        and "/" not in name
        and "\\" not in name
        and Path(name).name == name
    )


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(fd, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def staged_package_identity(payloads: Mapping[str, bytes]) -> str:
    """Return the content identity that must be retained outside a staged package."""

    if not payloads or any(
        not _safe_member_name(name) or type(payload) is not bytes
        for name, payload in payloads.items()
    ):
        raise ProductionJobError("staged package identity input is invalid")
    inventory = {
        name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        for name, payload in payloads.items()
    }
    return hashlib.sha256(
        _STAGED_PACKAGE_IDENTITY_DOMAIN + canonical_json_bytes(inventory)
    ).hexdigest()


def _open_staged_member(
    directory_fd: int, name: str, label: str, expected_fingerprint: tuple[int, ...]
) -> tuple[int, tuple[int, ...]]:
    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        member_fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ProductionJobError(f"staged {label} member is unsafe") from exc
    try:
        before = os.fstat(member_fd)
        path_before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        fingerprint = _stat_fingerprint(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o222
            or fingerprint != _stat_fingerprint(path_before)
        ):
            raise ProductionJobError(f"staged {label} member is unsafe")
        if fingerprint != expected_fingerprint:
            raise ProductionJobError(f"staged {label} member changed during read")
        return member_fd, fingerprint
    except OSError as exc:
        os.close(member_fd)
        raise ProductionJobError(f"staged {label} member is unsafe") from exc
    except BaseException:
        os.close(member_fd)
        raise


def _read_staged_members(
    target: Path,
    allowed_shapes: frozenset[frozenset[str]],
    label: str,
    expected_package_identity: str,
) -> dict[str, bytes]:
    if (
        not isinstance(expected_package_identity, str)
        or SHA256.fullmatch(expected_package_identity) is None
    ):
        raise ProductionJobError(f"staged {label} package identity is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(target, flags)
    except OSError as exc:
        raise ProductionJobError(f"staged {label} directory is unsafe") from exc
    members: dict[str, tuple[int, tuple[int, ...]]] = {}
    try:
        before = os.fstat(directory_fd)
        names = os.listdir(directory_fd)
        shape = frozenset(names)
        if (
            not stat.S_ISDIR(before.st_mode)
            or len(names) != len(shape)
            or any(not _safe_member_name(name) for name in names)
            or shape not in allowed_shapes
        ):
            raise ProductionJobError(f"staged {label} member set is invalid")
        ordered_names = ["identity.json", *sorted(shape - {"identity.json"})]
        expected_stats = {
            name: os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            for name in ordered_names
        }
        # The directory is sealed after every member; its ctime is the package-wide baseline.
        if any(value.st_ctime_ns > before.st_ctime_ns for value in expected_stats.values()):
            raise ProductionJobError(f"staged {label} member changed during read")
        expected_fingerprints = {
            name: _stat_fingerprint(value) for name, value in expected_stats.items()
        }
        for name in ordered_names:
            members[name] = _open_staged_member(
                directory_fd, name, label, expected_fingerprints[name]
            )
        for name, (member_fd, fingerprint) in members.items():
            if (
                _stat_fingerprint(os.fstat(member_fd)) != fingerprint
                or _stat_fingerprint(
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                )
                != fingerprint
            ):
                raise ProductionJobError(f"staged {label} member changed during read")
        payloads = {name: _read_fd(member_fd) for name, (member_fd, _) in members.items()}
        for name, (member_fd, _) in members.items():
            os.lseek(member_fd, 0, os.SEEK_SET)
            if _read_fd(member_fd) != payloads[name]:
                raise ProductionJobError(f"staged {label} member changed during read")
        for name, (member_fd, fingerprint) in members.items():
            if (
                _stat_fingerprint(os.fstat(member_fd)) != fingerprint
                or _stat_fingerprint(
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                )
                != fingerprint
            ):
                raise ProductionJobError(f"staged {label} member changed during read")
        after = os.fstat(directory_fd)
        target_after = os.stat(target, follow_symlinks=False)
        if (
            frozenset(os.listdir(directory_fd)) != shape
            or _stat_fingerprint(before) != _stat_fingerprint(after)
            or _stat_fingerprint(before) != _stat_fingerprint(target_after)
        ):
            raise ProductionJobError(f"staged {label} directory changed during read")
        if staged_package_identity(payloads) != expected_package_identity:
            raise ProductionJobError(f"staged {label} package identity mismatch")
        return payloads
    except OSError as exc:
        raise ProductionJobError(f"staged {label} directory is unsafe") from exc
    finally:
        for member_fd, _ in members.values():
            os.close(member_fd)
        os.close(directory_fd)


def _staged_identity(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionJobError(f"staged {label} identity is invalid") from exc
    if not isinstance(value, dict):
        raise ProductionJobError(f"staged {label} identity is invalid")
    return value


class ProductionJob(Protocol):
    job_id: str
    production_manifest_sha256: str

    def acquire(self, client: ProviderClient, scheduled_for: datetime) -> tuple[str, bytes]: ...

    def compute(
        self,
        raw: bytes,
        provider_url: str,
        scheduled_for: datetime,
    ) -> JobComputation: ...


class NoNetworkProductionJob(Protocol):
    job_id: str
    production_manifest_sha256: str

    def acquire_no_network(self, request_id: str) -> ProductionInput: ...

    def compute_no_network(
        self, value: ProductionInput, request_id: str
    ) -> FormalComputation: ...


def parse_manifest(
    payload: bytes,
    *,
    expected_sha256: str,
    model_id: str,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ProductionJobError("production manifest byte identity mismatch")
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionJobError("production manifest is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("stage") != "PRODUCTION_FROZEN"
        or value.get("model_id") != model_id
        or value.get("parameters") != dict(parameters)
        or value.get("execution", {}).get("automatic_ordering") is not False
    ):
        raise ProductionJobError("production manifest semantics mismatch")
    return value


def fit_next_log(values: Sequence[float], window: int) -> float:
    if len(values) != window or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ProductionJobError("trend window must contain finite positive values")
    logs = [math.log(value) for value in values]
    x_mean = (window - 1) / 2.0
    y_mean = sum(logs) / window
    denominator = sum((index - x_mean) ** 2 for index in range(window))
    slope = sum((index - x_mean) * (logs[index] - y_mean) for index in range(window))
    return y_mean + slope / denominator * (window - x_mean)


def decision_points(rows: Sequence[Mapping[str, Any]], config: TrendConfig) -> list[dict[str, Any]]:
    values = [float(row["signal_close"]) for row in rows]
    alpha = 2.0 / (config.ema_span + 1.0)
    smoothed: float | None = None
    previous: float | None = None
    points: list[dict[str, Any]] = []
    for position in range(config.window_sessions, len(rows) + 1):
        raw = fit_next_log(values[position - config.window_sessions : position], config.window_sessions)
        smoothed = raw if smoothed is None else alpha * raw + (1 - alpha) * smoothed
        slope = None if previous is None else 100.0 * math.expm1(smoothed - previous)
        points.append(
            {
                "decision_date": rows[position]["date"] if position < len(rows) else None,
                "is_next_session": position == len(rows),
                "raw_curve": math.exp(raw),
                "smooth_curve": math.exp(smoothed),
                "slope_pct": slope,
            }
        )
        previous = smoothed
    return points


def evaluate(points: Sequence[Mapping[str, Any]], config: TrendConfig) -> dict[str, Any]:
    eligible = [
        point
        for point in points
        if point["is_next_session"]
        or (
            isinstance(point["decision_date"], date)
            and point["decision_date"] >= config.anchor_date
        )
    ]
    state = 0
    prior_slope: float | None = None
    state_before = 0
    action = "WAIT"
    reason = "no upward crossing; remain flat"
    for point in eligible:
        value = point["slope_pct"]
        if value is None:
            continue
        slope = float(value)
        if point["is_next_session"]:
            state_before = state
            if prior_slope is not None:
                if state == 0 and prior_slope < config.buy_threshold_pct_per_day <= slope:
                    state, action, reason = 1, "BUY", "signal crossed upward through the frozen buy line"
                elif state == 1 and prior_slope > config.sell_threshold_pct_per_day >= slope:
                    state, action, reason = 0, "SELL", "signal crossed downward through the frozen sell line"
                elif state == 1:
                    action, reason = "HOLD", "signal did not cross the frozen sell line"
            break
        if prior_slope is not None:
            if state == 0 and prior_slope < config.buy_threshold_pct_per_day <= slope:
                state = 1
            elif state == 1 and prior_slope > config.sell_threshold_pct_per_day >= slope:
                state = 0
        prior_slope = slope
    if not eligible or eligible[-1]["is_next_session"] is not True or prior_slope is None:
        raise ProductionJobError("history cannot produce the next-session decision")
    point = eligible[-1]
    return {
        "action": action,
        "reason": reason,
        "state_before_next": state_before,
        "target_state": state,
        "previous_slope_pct": prior_slope,
        "next_slope_pct": float(point["slope_pct"]),
        "next_raw_curve": float(point["raw_curve"]),
        "next_smooth_curve": float(point["smooth_curve"]),
    }


def projected_slope(
    rows: Sequence[Mapping[str, Any]], config: TrendConfig, candidate_raw_close: float
) -> float:
    if not math.isfinite(candidate_raw_close) or candidate_raw_close <= 0:
        raise ProductionJobError("candidate close must be finite and positive")
    points = decision_points(rows, config)
    current = math.log(float(points[-1]["smooth_curve"]))
    factor = float(rows[-1]["signal_close"]) / float(rows[-1]["close"])
    values = [float(row["signal_close"]) for row in rows[-(config.window_sessions - 1) :]]
    values.append(candidate_raw_close * factor)
    next_raw = fit_next_log(values, config.window_sessions)
    alpha = 2.0 / (config.ema_span + 1.0)
    next_smooth = alpha * next_raw + (1 - alpha) * current
    return 100.0 * math.expm1(next_smooth - current)


def close_for_slope(
    rows: Sequence[Mapping[str, Any]], config: TrendConfig, target: float
) -> float:
    if not math.isfinite(target) or target <= -100:
        raise ProductionJobError("target slope is invalid")
    low = math.log(float(rows[-1]["close"])) - 50
    high = math.log(float(rows[-1]["close"])) + 50
    for _ in range(180):
        middle = (low + high) / 2
        if projected_slope(rows, config, math.exp(middle)) < target:
            low = middle
        else:
            high = middle
    return math.exp((low + high) / 2)


def normalized_rows(rows: Sequence[Mapping[str, Any]]) -> CanonicalJsonBytes:
    serializable = [
        {key: (value.isoformat() if isinstance(value, date) else value) for key, value in row.items()}
        for row in rows
    ]
    return CanonicalJsonBytes(canonical_json_bytes(serializable))


def identity(domain: bytes, value: Any) -> str:
    return hashlib.sha256(domain + canonical_json_bytes(value)).hexdigest()


def identity_canonical_bytes(domain: bytes, value: CanonicalJsonBytes) -> str:
    if type(value) is not CanonicalJsonBytes:
        raise TypeError("already-canonical identity requires CanonicalJsonBytes")
    return hashlib.sha256(domain + value.value).hexdigest()


def render_private_report(
    *,
    display_name: str,
    report_uuid: str,
    qualification: str,
    action: Mapping[str, Any],
    model_id: str,
    costs: str,
) -> bytes:
    encoded_action = html.escape(json.dumps(action, sort_keys=True, ensure_ascii=False), quote=True)
    document = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(display_name)} · Daily decision evidence</title></head><body>"
        f"<main data-report-uuid=\"{report_uuid}\"><h1>{html.escape(display_name)}</h1>"
        f"<p data-qualification=\"{qualification}\">{qualification}</p>"
        f"<p data-model=\"{model_id}\">{html.escape(costs)}; automatic_ordering=false; not investment advice.</p>"
        f"<pre data-action=\"canonical\">{encoded_action}</pre></main></body></html>\n"
    )
    return document.encode("utf-8")


def next_weekday(value: date) -> date:
    result = value + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


class ProductionJobs:
    """Production computation interface, including its durable staging representation."""

    def __init__(self, jobs: Sequence[ProductionJob | NoNetworkProductionJob]):
        self._jobs: dict[str, Any] = {job.job_id: job for job in jobs}
        if len(self._jobs) != len(jobs):
            raise ProductionJobError("production job IDs must be unique")

    def acquire(
        self, job_id: str, client: ProviderClient, scheduled_for: datetime
    ) -> tuple[str, bytes]:
        try:
            job = self._jobs[job_id]
        except KeyError as exc:
            raise ProductionJobError("production job is not registered") from exc
        return job.acquire(client, scheduled_for)

    def stage_input(
        self,
        job_id: str,
        client: ProviderClient,
        scheduled_for: datetime | None,
        *,
        request_id: str,
    ) -> ProductionInput:
        try:
            job = self._jobs[job_id]
        except KeyError as exc:
            raise ProductionJobError("production job is not registered") from exc
        acquire_no_network = getattr(job, "acquire_no_network", None)
        if acquire_no_network is not None:
            return acquire_no_network(request_id)
        if scheduled_for is None:
            raise ProductionJobError("provider job requires a scheduled invocation time")
        url, raw = job.acquire(client, scheduled_for)
        return ProductionInput(
            "provider-get", {"method": "GET", "provider_url": url}, raw
        )

    @staticmethod
    def input_payloads(value: ProductionInput) -> dict[str, bytes]:
        return {
            "identity.json": canonical_json_bytes(
                {"kind": value.kind, **dict(value.identity)}
            ),
            "raw.bin": value.payload,
        }

    @classmethod
    def read_input(cls, target: Path, *, expected_package_identity: str) -> ProductionInput:
        payloads = _read_staged_members(
            target,
            frozenset({_INPUT_MEMBERS}),
            "production input",
            expected_package_identity,
        )
        identity_bytes = payloads["identity.json"]
        identity = _staged_identity(identity_bytes, "production input")
        legacy_identity = "kind" not in identity or (
            identity.get("kind", "provider-get") == "provider-get" and "method" not in identity
        )
        kind = identity.pop("kind", "provider-get")
        if kind == "provider-get" and "method" not in identity:
            identity["method"] = "GET"
        value = ProductionInput(kind, identity, payloads["raw.bin"])
        expected = cls.input_payloads(value)
        if not legacy_identity and payloads != expected:
            raise ProductionJobError("staged production input read-back differs")
        return value

    def compute(
        self,
        job_id: str,
        raw: bytes,
        provider_url: str,
        scheduled_for: datetime,
    ) -> JobComputation:
        try:
            job = self._jobs[job_id]
        except KeyError as exc:
            raise ProductionJobError("production job is not registered") from exc
        return job.compute(raw, provider_url, scheduled_for)

    def compute_input(
        self,
        job_id: str,
        value: ProductionInput,
        scheduled_for: datetime | None,
        *,
        request_id: str,
    ) -> JobComputation | FormalComputation:
        try:
            job = self._jobs[job_id]
        except KeyError as exc:
            raise ProductionJobError("production job is not registered") from exc
        if value.kind == "no-network-operation":
            compute_no_network = getattr(job, "compute_no_network", None)
            if compute_no_network is None:
                raise ProductionJobError("job does not support a no-network operation")
            return compute_no_network(value, request_id)
        if scheduled_for is None or value.identity.get("method") != "GET":
            raise ProductionJobError("provider input identity is invalid")
        provider_url = value.identity.get("provider_url")
        if not isinstance(provider_url, str):
            raise ProductionJobError("provider input URL is invalid")
        return self.compute(job_id, value.payload, provider_url, scheduled_for)

    @staticmethod
    def computation_payloads(value: ProductionComputation) -> dict[str, bytes]:
        if isinstance(value, FormalComputation):
            identity = {
                "kind": "formal",
                "job_id": value.job_id,
                "production_manifest_sha256": value.production_manifest_sha256,
                "operation": value.operation,
                "authority_sha256": value.authority_sha256,
                "experiment_id": value.experiment_id,
                "attempt_id": value.attempt_id,
                "files": sorted(value.files),
            }
            return {"identity.json": canonical_json_bytes(identity), **dict(value.files)}
        identity = {
            "kind": "daily",
            "job_id": value.job_id,
            "model_id": value.model_id,
            "production_manifest_sha256": value.production_manifest_sha256,
            "report_uuid": value.report_uuid,
            "provider_url": value.provider_url,
            "raw_name": value.raw_name,
            "experiment_id": value.experiment_id,
            "attempt_id": value.attempt_id,
        }
        return {
            "identity.json": canonical_json_bytes(identity),
            "raw.bin": value.raw_bytes,
            "normalized.json": value.normalized_bytes,
            "action.json": canonical_json_bytes(value.action),
            "report.html": value.report_html,
            "notification.txt": value.notification_bytes,
        }

    @classmethod
    def read_computation(
        cls, target: Path, *, expected_package_identity: str
    ) -> ProductionComputation:
        payloads = _read_staged_members(
            target,
            frozenset({_DAILY_COMPUTATION_MEMBERS, _FORMAL_COMPUTATION_MEMBERS}),
            "production computation",
            expected_package_identity,
        )
        identity = _staged_identity(payloads["identity.json"], "production computation")
        if identity.get("kind") == "formal":
            if (
                frozenset(payloads) != _FORMAL_COMPUTATION_MEMBERS
                or identity.get("files") != sorted(_FORMAL_RESULT_MEMBERS)
            ):
                raise ProductionJobError(
                    "staged production computation member set is invalid"
                )
            value: ProductionComputation = FormalComputation(
                job_id=identity["job_id"],
                production_manifest_sha256=identity["production_manifest_sha256"],
                operation=identity["operation"],
                authority_sha256=identity["authority_sha256"],
                files={name: payloads[name] for name in identity["files"]},
                experiment_id=identity["experiment_id"],
                attempt_id=identity["attempt_id"],
            )
        else:
            if (
                identity.get("kind") != "daily"
                or frozenset(payloads) != _DAILY_COMPUTATION_MEMBERS
            ):
                raise ProductionJobError(
                    "staged production computation member set is invalid"
                )
            value = JobComputation(
                identity["job_id"],
                identity["model_id"],
                identity["production_manifest_sha256"],
                identity["report_uuid"],
                identity["provider_url"],
                identity["raw_name"],
                payloads["raw.bin"],
                payloads["normalized.json"],
                json.loads(payloads["action.json"]),
                payloads["report.html"],
                payloads["notification.txt"],
                identity["experiment_id"],
                identity["attempt_id"],
            )
        expected = cls.computation_payloads(value)
        if payloads != expected:
            raise ProductionJobError("staged production computation read-back differs")
        return value
