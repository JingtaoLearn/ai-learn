# PostgreSQL Operator schema and migration mapping

Schema identity: `quantresearch-postgresql-operator-v1` in `qr.schema_identity`.

| Existing Operator authority | PostgreSQL destination | Preservation rule |
|---|---|---|
| `operators` | `qr.operators` | Immutable operator identity, slot, title, summary, and timezone-aware creation time. |
| `operator_versions` | `qr.operator_versions` | Immutable semantic version, action identity (`operator_id@version`), content digest, JSONB schema/defaults/evidence, documentation, status, and artifact-set identity. |
| `operator_latest` | `qr.operator_current` | Transactional current projection, advanced only by semantic-version order. |
| Bundle file bytes | `qr.artifacts.payload` | Exact `bytea`, lowercase SHA-256, byte size, and media type checked by PostgreSQL. |
| Bundle member inventory | `qr.artifact_sets` + `qr.artifact_set_members` | Canonical JSONB manifest, digest-derived set identity, unique ordinal/name, and closed membership after publication. |

The offline importer opens a frozen `catalog.sqlite3` in read-only immutable mode, verifies the catalog and every bundle member against a create-only source manifest, requires an empty target, inserts once, then compares ordered canonical Operator and artifact-member digests after PostgreSQL read-back. It never updates or deletes source bytes and is not a runtime adapter.

The runtime role receives only `SELECT, INSERT` on immutable tables and `SELECT, INSERT, UPDATE` on `operator_current`; immutable triggers reject updates/deletes and a publication trigger rejects late artifact-set members. The migration role owns schema installation and grants.
