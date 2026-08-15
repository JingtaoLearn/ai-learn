import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))


class IntegrationAuditTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.skills_root = self.root / "skills"
        self.catalog_root = self.root / "categories"
        self.skills_root.mkdir()
        self.catalog_root.mkdir()
        category = {
            "schema_version": 1,
            "category_id": "investment-research",
            "title": "Investment Research",
            "subtitle": "投资研究",
            "description": "Research reports.",
            "icon": "📈",
            "accent": "teal",
            "sort_order": 10,
            "item_label": "份研究报告",
            "source_skill": "content-hub-registry",
        }
        (self.catalog_root / "investment-research.json").write_text(
            json.dumps(category), encoding="utf-8"
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def write_skill(self, name: str, body: str) -> None:
        path = self.skills_root / "research" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            f"---\nname: {name}\ndescription: Test skill.\n---\n\n{body}\n",
            encoding="utf-8",
        )

    def write_config(self, skills: dict) -> Path:
        path = self.root / "integrations.json"
        path.write_text(
            json.dumps({"schema_version": 1, "skills": skills}),
            encoding="utf-8",
        )
        return path

    def test_direct_integration_requires_new_registry_and_known_category(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** direct · category=investment-research · identity=stable\n\n"
            "After publishing, load `content-hub-registry` and register the report.",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        self.assertEqual(
            audit_integrations(config, self.skills_root, self.catalog_root), []
        )

    def test_audit_rejects_contract_marked_skill_omitted_from_catalog(self):
        from audit_integrations import audit_integrations

        body = (
            "**Content Hub contract:** direct · category=investment-research · identity=stable\n\n"
            "Load `content-hub-registry` and register the report."
        )
        self.write_skill("stock-report", body)
        self.write_skill("wealth-report", body)
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("wealth-report" in error and "omitted" in error for error in errors))

    def test_audit_rejects_any_legacy_finance_registry_reference(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "Call investment-research-registry and register_report.py.",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("legacy" in error.lower() for error in errors))
        self.assertTrue(any("content-hub-registry" in error for error in errors))

    def test_audit_scans_skill_reference_files_for_legacy_registry(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** direct · category=investment-research · identity=stable\n\n"
            "After publishing, load `content-hub-registry` and register the report.",
        )
        reference = (
            self.skills_root
            / "research"
            / "stock-report"
            / "references"
            / "legacy.txt"
        )
        reference.parent.mkdir()
        reference.write_text("Run register_report.py", encoding="utf-8")
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("references/legacy.txt" in error for error in errors))

    def test_audit_fails_closed_on_nonbinary_invalid_utf8_support_file(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** direct · category=investment-research · identity=stable\n\n"
            "Load `content-hub-registry` and register the report.",
        )
        support = (
            self.skills_root
            / "research"
            / "stock-report"
            / "references"
            / "legacy.blob"
        )
        support.parent.mkdir()
        support.write_bytes(b"register_report.py\xff")
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("valid UTF-8" in error for error in errors))

    def test_audit_rejects_legacy_finance_hub_wording(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** direct · category=investment-research · identity=stable\n\n"
            "Load `content-hub-registry`. Also register to finance hub.",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("finance hub" in error for error in errors))

    def test_audit_rejects_descriptions_over_sixty_characters(self):
        from audit_integrations import audit_integrations

        path = self.skills_root / "research" / "stock-report" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\nname: stock-report\ndescription: "
            + ("x" * 61)
            + "\n---\n\nLoad content-hub-registry.\n",
            encoding="utf-8",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("description" in error for error in errors))

    def test_audit_rejects_policy_marker_or_identity_mismatch(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** direct · category=investment-research · identity=stable\n\n"
            "Load `content-hub-registry` and register the report.",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "collection",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("collection" in error for error in errors))
        self.assertTrue(any("contract" in error for error in errors))

    def test_audit_rejects_unknown_policy_fields_and_boolean_schema(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** direct · category=investment-research · identity=stable\n\n"
            "Load `content-hub-registry` and register the report.",
        )
        config = self.root / "integrations.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": True,
                    "skills": {
                        "stock-report": {
                            "mode": "direct",
                            "category_id": "investment-research",
                            "identity_policy": "stable",
                            "unexpected": "value",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("schema_version" in error for error in errors))
        self.assertTrue(any("fields" in error for error in errors))

    def test_audit_rejects_direct_contract_without_load_instruction(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** direct · category=investment-research · identity=stable",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("explicitly load" in error for error in errors))

    def test_audit_rejects_direct_policy_with_collection_workflow(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** direct · category=investment-research · identity=stable\n\n"
            "Load `content-hub-registry`. Do not register every report; maintain one collection entry.",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("collection workflow" in error for error in errors))

    def test_audit_rejects_conflicting_contract_markers(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** direct · category=investment-research · identity=stable\n"
            "**Content Hub contract:** collection · category=investment-research · identity=collection\n\n"
            "Load `content-hub-registry` and register the report.",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("exactly one contract" in error for error in errors))

    def test_audit_rejects_collection_without_no_per_item_rule(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** collection · category=investment-research · identity=collection\n\n"
            "Load `content-hub-registry` and register each report.",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "collection",
                    "category_id": "investment-research",
                    "identity_policy": "collection",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("every item" in error for error in errors))

    def test_audit_rejects_none_without_non_owner_statement(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** none\n\n"
            "Load `content-hub-registry` and register reports directly.",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "none",
                    "reason": "Publisher only.",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("does not register" in error for error in errors))

    def test_audit_rejects_none_with_conflicting_direct_instruction(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** none\n\n"
            "This Skill does not register previews. "
            "Register every finished report directly after publication.",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "none",
                    "reason": "Publisher only.",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("direct registration" in error for error in errors))

    def test_audit_rejects_collection_with_positive_per_item_instruction(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** collection · category=investment-research · identity=collection\n\n"
            "Load `content-hub-registry`. Do not register every item; register every report too.",
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "collection",
                    "category_id": "investment-research",
                    "identity_policy": "collection",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("per-item" in error for error in errors))

    def test_audit_rejects_malformed_category_catalog_entry(self):
        from audit_integrations import audit_integrations

        self.write_skill(
            "stock-report",
            "**Content Hub contract:** direct · category=investment-research · identity=stable\n\n"
            "Load `content-hub-registry` and register the report.",
        )
        malformed = json.loads(
            (self.catalog_root / "investment-research.json").read_text()
        )
        malformed["schema_version"] = True
        (self.catalog_root / "investment-research.json").write_text(
            json.dumps(malformed), encoding="utf-8"
        )
        config = self.write_config(
            {
                "stock-report": {
                    "mode": "direct",
                    "category_id": "investment-research",
                    "identity_policy": "stable",
                }
            }
        )

        errors = audit_integrations(config, self.skills_root, self.catalog_root)

        self.assertTrue(any("category" in error.lower() for error in errors))


if __name__ == "__main__":
    unittest.main()
