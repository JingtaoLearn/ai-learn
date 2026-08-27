import json
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from quant_platform.catalog import Catalog, initialize_catalog
from quant_platform.seed import BUILTIN_OPERATOR_IDS, TEMPLATE_NAME, TEMPLATE_VERSION


def test_initialization_enables_wal_constraints_and_idempotent_migrations(tmp_path: Path):
    state_root = tmp_path / "platform"

    with ThreadPoolExecutor(max_workers=4) as executor:
        catalogs = list(executor.map(initialize_catalog, [state_root] * 4))

    assert all(catalog.database_path == state_root / "catalog.sqlite3" for catalog in catalogs)
    with sqlite3.connect(state_root / "catalog.sqlite3") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM templates WHERE name = ? AND version = ?",
            (TEMPLATE_NAME, TEMPLATE_VERSION),
        ).fetchone() == (1,)

    connection = catalogs[0].connect()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    finally:
        connection.close()


def test_seeded_template_has_exact_immutable_slot_and_parameter_ownership(tmp_path: Path):
    catalog = initialize_catalog(tmp_path / "state")

    template = catalog.template_detail(TEMPLATE_NAME, TEMPLATE_VERSION)

    assert template["name"] == "single_stock_daily_causal"
    assert template["version"] == "1"
    assert template["slots"] == [
        "fit",
        "smoothing",
        "statistic",
        "decision",
        "sizing",
        "cost",
        "report",
    ]
    assert set(template["parameter_schema"]["properties"]) == {
        "instrument_display_name",
        "evaluation_start",
        "evaluation_end",
        "initial_capital_cny",
        "initial_state",
        "terminal_handling",
        "cost_assumption_label",
    }
    assert len(template["content_digest"]) == 64

    with catalog.transaction(immediate=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE templates SET version = '2' WHERE name = ? AND version = ?",
                (TEMPLATE_NAME, TEMPLATE_VERSION),
            )


def test_all_builtins_are_published_immutable_versions_with_bocom_defaults(tmp_path: Path):
    catalog = initialize_catalog(tmp_path / "state")

    operators = catalog.list_operators()

    assert {operator["operator_id"] for operator in operators} == set(BUILTIN_OPERATOR_IDS)
    assert all(operator["latest_version"] == "1.0.0" for operator in operators)
    fit = catalog.operator_detail("prior_log_ols", "1.0.0")
    assert fit["status"] == "PUBLISHED"
    assert fit["defaults"] == {"price_column": "AdjustedClose", "window_sessions": 20}
    assert fit["title_zh"]
    assert fit["summary_zh"]
    assert fit["documentation"]
    assert len(fit["content_digest"]) == 64
    assert fit["validation_evidence"]["kind"] == "trusted_builtin"

    bundle = catalog.state_root / fit["bundle_path"]
    assert bundle.is_dir()
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o555
    assert {path.name for path in bundle.iterdir()} == {"manifest.json"}
    assert stat.S_IMODE((bundle / "manifest.json").stat().st_mode) == 0o444
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["content_digest"] == fit["content_digest"]


def test_latest_pointer_uses_published_numeric_semantic_version(tmp_path: Path):
    catalog = initialize_catalog(tmp_path / "state")
    schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    catalog.insert_operator_version_for_test(
        operator_id="numeric-version",
        slot="fit",
        version="1.9.0",
        content_digest="1" * 64,
        parameter_schema=schema,
    )
    catalog.insert_operator_version_for_test(
        operator_id="numeric-version",
        slot="fit",
        version="1.10.0",
        content_digest="2" * 64,
        parameter_schema=schema,
    )
    catalog.insert_operator_version_for_test(
        operator_id="numeric-version",
        slot="fit",
        version="2.0.0",
        content_digest="3" * 64,
        parameter_schema=schema,
        status="REJECTED",
    )

    assert catalog.operator_detail("numeric-version")["version"] == "1.10.0"
    assert catalog.operator_detail("numeric-version", "1.9.0")["version"] == "1.9.0"


def test_database_unique_constraints_cover_domain_convergence(tmp_path: Path):
    catalog = initialize_catalog(tmp_path / "state")
    connection = catalog.connect()
    try:
        connection.execute(
            "INSERT INTO replay_tokens(token_hash, expires_at) VALUES (?, ?)",
            ("a" * 64, 1_900_000_000),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO replay_tokens(token_hash, expires_at) VALUES (?, ?)",
                ("a" * 64, 1_900_000_000),
            )
        connection.execute(
            "INSERT INTO experiments(experiment_id, identity_json, created_at) VALUES (?, ?, ?)",
            ("b" * 64, "{}", "2026-08-27T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO experiments(experiment_id, identity_json, created_at) VALUES (?, ?, ?)",
                ("b" * 64, "{}", "2026-08-27T00:00:00Z"),
            )
        connection.execute(
            """
            INSERT INTO attempts(
                attempt_id, experiment_id, action_id, sequence, status, requested_json,
                resolved_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("attempt-1", "b" * 64, "action-1", 1, "PENDING", "{}", "{}", "2026-08-27T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, experiment_id, action_id, sequence, status, requested_json,
                    resolved_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "attempt-2",
                    "b" * 64,
                    "action-1",
                    2,
                    "PENDING",
                    "{}",
                    "{}",
                    "2026-08-27T00:00:00Z",
                ),
            )
    finally:
        connection.close()


def test_attempt_launch_count_is_database_constrained_to_zero_or_one(tmp_path: Path):
    catalog = initialize_catalog(tmp_path / "state")
    with catalog.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO experiments(experiment_id, identity_json, created_at) VALUES (?, ?, ?)",
            ("d" * 64, "{}", "2026-08-27T00:00:00Z"),
        )


@pytest.mark.parametrize(
    "status",
    [
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "INTERRUPTED",
        "TERMINATION_UNCONFIRMED",
    ],
)
def test_attempt_schema_accepts_only_declared_lifecycle_states(
    tmp_path: Path, status: str
):
    catalog = initialize_catalog(tmp_path / status)
    with catalog.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO experiments(experiment_id, identity_json, created_at) VALUES (?, ?, ?)",
            ("e" * 64, "{}", "2026-08-27T00:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO attempts(
        attempt_id, experiment_id, action_id, sequence, status,
        requested_json, resolved_json, created_at
            ) VALUES (?, ?, ?, 1, ?, '{}', '{}', ?)
            """,
            (f"attempt-{status}", "e" * 64, f"action-{status}", status, "2026-08-27T00:00:00Z"),
        )

    with catalog.transaction(immediate=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
        """
        INSERT INTO attempts(
            attempt_id, experiment_id, action_id, sequence, status,
            requested_json, resolved_json, created_at
        ) VALUES ('invalid', ?, 'invalid', 2, 'RETRYING', '{}', '{}', ?)
        """,
        ("e" * 64, "2026-08-27T00:00:00Z"),
            )
        for launch_count in (-1, 2):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                connection.execute(
                    """
                    INSERT INTO attempts(
                        attempt_id, experiment_id, action_id, sequence, status,
                        requested_json, resolved_json, created_at, launch_count
                    ) VALUES (?, ?, ?, 1, 'PENDING', '{}', '{}', ?, ?)
                    """,
                    (
                        f"attempt-{launch_count}",
                        "d" * 64,
                        f"action-{launch_count}",
                        "2026-08-27T00:00:00Z",
                        launch_count,
                    ),
                )


def test_catalog_rejects_symlink_state_root(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        Catalog(linked).initialize()
