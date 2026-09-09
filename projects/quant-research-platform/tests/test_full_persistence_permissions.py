from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast

from quant_platform import full_persistence


class _Cursor:
    def fetchone(self):
        return {"identity": full_persistence.FULL_SCHEMA_IDENTITY}


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def transaction(self):
        return nullcontext()

    def execute(self, statement: str, parameters=()):
        self.statements.append(" ".join(statement.split()))
        return _Cursor()


class _Config:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self):
        return self.connection


def test_schema_installer_keeps_replay_tokens_purgeable_but_not_updatable(
    monkeypatch, tmp_path: Path
) -> None:
    connection = _Connection()
    monkeypatch.setattr(full_persistence, "migrate_schema", lambda *args, **kwargs: None)

    full_persistence.install_full_schema(
        cast(Any, _Config(connection)),
        runtime_user="qr_runtime",
        runtime_password_file=tmp_path / "runtime-password",
    )

    statements = connection.statements
    assert not any(
        'CREATE TRIGGER "replay_tokens_immutable"' in statement
        for statement in statements
    )
    assert (
        'DROP TRIGGER IF EXISTS "replay_tokens_immutable" '
        'ON qr_catalog.replay_tokens'
    ) in statements
    assert (
        'REVOKE UPDATE ON qr_catalog.replay_tokens FROM "qr_runtime"'
    ) in statements
    assert (
        'GRANT DELETE ON qr_catalog.replay_tokens TO "qr_runtime"'
    ) in statements
