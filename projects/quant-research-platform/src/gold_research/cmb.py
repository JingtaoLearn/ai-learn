from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

CMB_GOLD_API_URL = "https://m.cmbchina.com/api/rate/gold"
SOURCE_KIND = "sge_market_snapshot_via_cmb_public_page"
SNAPSHOT_COLUMNS = {
    "market_timestamp",
    "retrieved_at_utc",
    "variety",
    "gold_no",
    "current_price",
    "change",
    "open",
    "previous_close",
    "high",
    "low",
    "average_price",
    "trade_count",
    "source_url",
    "source_kind",
    "is_executable_cmb_gold_account_quote",
    "payload_sha256",
}


class CmbGoldDataError(RuntimeError):
    """Raised when the public CMB gold market snapshot cannot be validated."""


def _number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise CmbGoldDataError(f"invalid {field}: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CmbGoldDataError(f"invalid {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise CmbGoldDataError(f"invalid {field}: {value!r}")
    return number


def _integer(value: object, field: str) -> int:
    number = _number(value, field)
    if not number.is_integer() or number < 0:
        raise CmbGoldDataError(f"invalid {field}: {value!r}")
    return int(number)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CmbGoldDataError(f"invalid {field}: {value!r}")
    return value.strip()


def parse_cmb_gold_payload(payload: dict, *, retrieved_at: datetime) -> pd.DataFrame:
    """Parse CMB's public SGE market snapshot.

    This payload contains Shanghai Gold Exchange reference-market fields. It is
    not the authenticated CMB Gold Account purchase or redemption quote.
    """

    if not isinstance(payload, dict):
        raise CmbGoldDataError("CMB gold API response must be a JSON object")
    code = payload.get("returnCode")
    if code != "SUC0000":
        raise CmbGoldDataError(f"CMB gold API returned {code}: {payload.get('errorMsg')}")
    body = payload.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise CmbGoldDataError("CMB gold API response has no body.data list")
    try:
        market_date = pd.Timestamp(body["time"]).date()
    except (KeyError, TypeError, ValueError) as exc:
        raise CmbGoldDataError("CMB gold API response has an invalid market time") from exc

    retrieved = pd.Timestamp(retrieved_at)
    if retrieved.tzinfo is None:
        retrieved = retrieved.tz_localize("UTC")
    else:
        retrieved = retrieved.tz_convert("UTC")
    payload_sha256 = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()

    rows = []
    for item in body["data"]:
        if not isinstance(item, dict):
            raise CmbGoldDataError("CMB gold API contains a non-object row")
        try:
            market_timestamp = pd.Timestamp(f"{market_date} {item['time']}", tz="Asia/Shanghai")
            row = {
                "market_timestamp": market_timestamp,
                "retrieved_at_utc": retrieved,
                "variety": _text(item.get("variety"), "variety"),
                "gold_no": _text(item.get("goldNo"), "goldNo"),
                "current_price": _number(item.get("curPrice"), "curPrice"),
                "change": _number(item.get("upDown"), "upDown"),
                "open": _number(item.get("open"), "open"),
                "previous_close": _number(item.get("preClose"), "preClose"),
                "high": _number(item.get("high"), "high"),
                "low": _number(item.get("low"), "low"),
                "average_price": _number(item.get("avePrice"), "avePrice"),
                "trade_count": _integer(item.get("tradeCount"), "tradeCount"),
                "source_url": CMB_GOLD_API_URL,
                "source_kind": SOURCE_KIND,
                "is_executable_cmb_gold_account_quote": False,
                "payload_sha256": payload_sha256,
            }
        except KeyError as exc:
            raise CmbGoldDataError(f"CMB gold API row is missing {exc.args[0]}") from exc
        rows.append(row)

    if not rows:
        raise CmbGoldDataError("CMB gold API response contains no market rows")
    return pd.DataFrame(rows)


def _validate_snapshot_frame(frame: pd.DataFrame) -> pd.DataFrame:
    clean = frame.copy()
    missing = SNAPSHOT_COLUMNS - set(clean.columns)
    if missing:
        raise CmbGoldDataError(f"snapshot frame is missing columns: {sorted(missing)}")
    if clean.empty:
        raise CmbGoldDataError("snapshot frame is empty")
    if not clean["source_kind"].eq(SOURCE_KIND).all():
        raise CmbGoldDataError("snapshot source_kind does not match the CMB SGE adapter")
    if not clean["source_url"].eq(CMB_GOLD_API_URL).all():
        raise CmbGoldDataError("snapshot source_url does not match the CMB public API")
    classification = clean["is_executable_cmb_gold_account_quote"]
    if not classification.map(lambda value: isinstance(value, (bool, np.bool_)) and not bool(value)).all():
        raise CmbGoldDataError("snapshot is incorrectly classified as an executable CMB quote")
    if not clean["payload_sha256"].astype(str).str.fullmatch(r"[0-9a-f]{64}").all():
        raise CmbGoldDataError("snapshot payload_sha256 is invalid")
    for column in [
        "current_price",
        "change",
        "open",
        "previous_close",
        "high",
        "low",
        "average_price",
        "trade_count",
    ]:
        values = pd.to_numeric(clean[column], errors="coerce")
        if values.isna().any() or not values.map(math.isfinite).all():
            raise CmbGoldDataError(f"snapshot {column} contains invalid numbers")
        clean[column] = values
    if (clean["trade_count"] < 0).any() or not clean["trade_count"].map(
        lambda value: float(value).is_integer()
    ).all():
        raise CmbGoldDataError("snapshot trade_count must contain non-negative integers")
    clean["trade_count"] = clean["trade_count"].astype(int)
    for column in ["gold_no", "variety"]:
        if clean[column].map(lambda value: not isinstance(value, str) or not value.strip()).any():
            raise CmbGoldDataError(f"snapshot {column} identifiers must be non-empty strings")
    try:
        clean["market_timestamp"] = pd.to_datetime(clean["market_timestamp"], utc=True).dt.tz_convert(
            "Asia/Shanghai"
        )
        clean["retrieved_at_utc"] = pd.to_datetime(clean["retrieved_at_utc"], utc=True)
    except (TypeError, ValueError) as exc:
        raise CmbGoldDataError(f"snapshot timestamps are invalid: {exc}") from exc
    if clean[["market_timestamp", "retrieved_at_utc"]].isna().any().any():
        raise CmbGoldDataError("snapshot timestamps must not be missing")
    return clean


def _reject_conflicting_duplicates(frame: pd.DataFrame) -> None:
    key = ["market_timestamp", "gold_no"]
    duplicates = frame[frame.duplicated(subset=key, keep=False)]
    comparison_columns = [
        "variety",
        "current_price",
        "change",
        "open",
        "previous_close",
        "high",
        "low",
        "average_price",
        "trade_count",
        "source_url",
        "source_kind",
        "is_executable_cmb_gold_account_quote",
    ]
    for _, group in duplicates.groupby(key, dropna=False):
        if any(group[column].nunique(dropna=False) > 1 for column in comparison_columns):
            raise CmbGoldDataError("conflicting duplicate CMB market rows")


def _load_existing_snapshot(csv_path: Path, parquet_path: Path, manifest_path: Path) -> pd.DataFrame:
    try:
        manifest = json.loads(manifest_path.read_text())
        csv_digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        parquet_digest = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
        if manifest.get("csv_sha256") != csv_digest or manifest.get("parquet_sha256") != parquet_digest:
            raise CmbGoldDataError("existing CMB snapshot integrity hash mismatch")
        csv_frame = pd.read_csv(csv_path)
        parquet_frame = pd.read_parquet(parquet_path)
        if len(csv_frame) != len(parquet_frame) or manifest.get("rows") != len(csv_frame):
            raise CmbGoldDataError("existing CMB snapshot integrity row-count mismatch")
        return _validate_snapshot_frame(csv_frame)
    except CmbGoldDataError:
        raise
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise CmbGoldDataError(f"existing CMB snapshot integrity check failed: {exc}") from exc


def fetch_cmb_gold_snapshot(timeout: int = 20) -> pd.DataFrame:
    """Fetch and parse the current public CMB SGE market snapshot."""

    try:
        response = requests.get(
            CMB_GOLD_API_URL,
            timeout=timeout,
            headers={
                "User-Agent": "gold-quant-research/0.1",
                "Referer": "https://m.cmbchina.com/goldrate.html",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise CmbGoldDataError(f"CMB gold API request failed: {exc}") from exc
    return parse_cmb_gold_payload(payload, retrieved_at=datetime.now(timezone.utc))


def append_cmb_gold_snapshot(frame: pd.DataFrame, output_dir: Path) -> dict:
    """Append one public-market snapshot and persist deterministic artifacts."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "cmb_sge_gold_snapshots.csv"
    parquet_path = output_dir / "cmb_sge_gold_snapshots.parquet"
    manifest_path = output_dir / "data_manifest.json"
    lock_path = output_dir / ".cmb_snapshot.lock"

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            return _append_cmb_gold_snapshot_locked(frame, csv_path, parquet_path, manifest_path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _append_cmb_gold_snapshot_locked(
    frame: pd.DataFrame, csv_path: Path, parquet_path: Path, manifest_path: Path
) -> dict:

    existing_paths = [csv_path.exists(), parquet_path.exists(), manifest_path.exists()]
    if any(existing_paths) and not all(existing_paths):
        raise CmbGoldDataError("CMB snapshot artifacts are incomplete; refusing to append")

    clean = _validate_snapshot_frame(frame)

    if csv_path.exists():
        existing = _load_existing_snapshot(csv_path, parquet_path, manifest_path)
        clean = pd.concat([existing, clean], ignore_index=True)
        clean = _validate_snapshot_frame(clean)

    _reject_conflicting_duplicates(clean)
    clean = (
        clean.drop_duplicates(subset=["market_timestamp", "gold_no"], keep="last")
        .sort_values(["market_timestamp", "gold_no"])
        .reset_index(drop=True)
    )
    with tempfile.TemporaryDirectory(dir=csv_path.parent) as staging_dir:
        staging = Path(staging_dir)
        staged_csv = staging / csv_path.name
        staged_parquet = staging / parquet_path.name
        staged_manifest = staging / manifest_path.name
        clean.to_csv(staged_csv, index=False, float_format="%.10g")
        clean.to_parquet(staged_parquet, index=False)

        csv_digest = hashlib.sha256(staged_csv.read_bytes()).hexdigest()
        parquet_digest = hashlib.sha256(staged_parquet.read_bytes()).hexdigest()
        manifest = {
            "source_url": CMB_GOLD_API_URL,
            "source_kind": SOURCE_KIND,
            "is_executable_cmb_gold_account_quote": False,
            "rows": int(len(clean)),
            "data_start": clean["market_timestamp"].min().isoformat(),
            "data_end": clean["market_timestamp"].max().isoformat(),
            "latest_retrieved_at_utc": clean["retrieved_at_utc"].max().isoformat(),
            "latest_payload_sha256": clean.sort_values("retrieved_at_utc").iloc[-1]["payload_sha256"],
            "csv": str(csv_path),
            "parquet": str(parquet_path),
            "csv_sha256": csv_digest,
            "parquet_sha256": parquet_digest,
        }
        staged_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.replace(staged_csv, csv_path)
        os.replace(staged_parquet, parquet_path)
        os.replace(staged_manifest, manifest_path)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect CMB's public SGE gold market snapshot")
    parser.add_argument("--output", type=Path, default=Path("data/cmb"))
    args = parser.parse_args()
    manifest = append_cmb_gold_snapshot(fetch_cmb_gold_snapshot(), args.output)
    print(json.dumps({"rows": manifest["rows"], "data_end": manifest["data_end"]}))


if __name__ == "__main__":
    main()
