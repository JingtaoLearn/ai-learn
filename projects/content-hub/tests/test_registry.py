import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


class ContentHubRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.category = {
            "schema_version": 1,
            "category_id": "skill-learning",
            "title": "Skill Learning",
            "subtitle": "双语技能学习",
            "description": "保留原始 SKILL.md，并提供双语阅读、语境词汇和设计拆解。",
            "icon": "📘",
            "accent": "terracotta",
            "sort_order": 20,
            "item_label": "份学习报告",
            "source_skill": "skill-learning-reports",
        }
        self.item = {
            "schema_version": 1,
            "category_id": "skill-learning",
            "item_id": "ask-matt-3d38910535f01e15bc5fd7f6ca8880d628cd248741f08e6780dd7c1828e832",
            "title": "ask-matt",
            "subtitle": "skills/engineering/ask-matt/SKILL.md",
            "published_at": "2026-08-10T08:22:49+08:00",
            "primary_url": "https://share.ai.jingtao.fun/9876dac2-cb2e-4234-8658-9f5fc000ef32.html?v=7",
            "source_url": "https://github.com/mattpocock/skills/blob/main/skills/engineering/ask-matt/SKILL.md",
            "summary": "把独立 skills 组织成按工作阶段导航、分流和汇流的流程地图。",
            "badges": ["Promoted"],
            "stats": [
                {"label": "序号", "value": "#02", "sub": "学习系列"},
                {"label": "版本", "value": "84fdeffd", "sub": "上游 commit"},
            ],
            "highlights": ["on-ramp", "leave a paper trail"],
            "tags": ["workflow", "engineering"],
            "source_skill": "skill-learning-reports",
        }

    def tearDown(self):
        self.tempdir.cleanup()

    @property
    def public_root(self):
        return self.root / "current"

    def test_category_registration_builds_level_one_and_level_two_pages(self):
        from register import register_category

        destination = register_category(self.category, self.root)

        self.assertEqual(
            destination,
            self.public_root / "categories" / "skill-learning" / "category.json",
        )
        self.assertTrue((self.public_root / "dashboard.html").is_file())
        self.assertTrue((self.public_root / "index.json").is_file())
        self.assertTrue((self.public_root / "categories" / "skill-learning" / "index.html").is_file())
        self.assertTrue((self.public_root / "categories" / "skill-learning" / "index.json").is_file())
        index = json.loads((self.public_root / "index.json").read_text())
        self.assertEqual(index["category_count"], 1)
        self.assertEqual(index["item_count"], 0)

    def test_item_registration_requires_existing_category_and_updates_both_levels(self):
        from register import RegistrationError, register_category, register_item

        with self.assertRaises(RegistrationError):
            register_item(self.item, self.root)

        register_category(self.category, self.root)
        destination = register_item(self.item, self.root)

        self.assertEqual(
            destination,
            self.public_root
            / "categories"
            / "skill-learning"
            / "items"
            / self.item["item_id"]
            / "card.json",
        )
        root_index = json.loads((self.public_root / "index.json").read_text())
        category_index = json.loads(
            (self.public_root / "categories" / "skill-learning" / "index.json").read_text()
        )
        self.assertEqual(root_index["item_count"], 1)
        self.assertEqual(category_index["item_count"], 1)
        self.assertEqual(category_index["items"][0]["item_id"], self.item["item_id"])

    def test_item_may_omit_external_source_link_with_empty_string(self):
        from register import register_category, register_item

        register_category(self.category, self.root)
        register_item(dict(self.item, source_url=""), self.root)
        category_html = (
            self.public_root / "categories" / "skill-learning" / "index.html"
        ).read_text()
        self.assertNotIn('class="source-link"', category_html)

    def test_item_registration_is_idempotent_for_same_category_and_item_id(self):
        from register import register_category, register_item

        register_category(self.category, self.root)
        register_item(self.item, self.root)
        register_item(dict(self.item, summary="Updated summary"), self.root)

        cards = list(self.public_root.glob("categories/*/items/*/card.json"))
        self.assertEqual(len(cards), 1)
        self.assertEqual(json.loads(cards[0].read_text())["summary"], "Updated summary")

    def test_registering_new_category_does_not_lose_existing_categories_or_items(self):
        from register import register_category, register_item

        register_category(self.category, self.root)
        register_item(self.item, self.root)
        finance = dict(
            self.category,
            category_id="investment-research",
            title="Investment Research",
            subtitle="投资研究",
            icon="📈",
            accent="teal",
            sort_order=10,
            item_label="份研究报告",
            source_skill="content-hub-registry",
        )
        register_category(finance, self.root)

        index = json.loads((self.public_root / "index.json").read_text())
        self.assertEqual(index["category_count"], 2)
        self.assertEqual(index["item_count"], 1)
        self.assertEqual(
            [entry["category_id"] for entry in index["categories"]],
            ["investment-research", "skill-learning"],
        )

    def test_registry_rejects_unknown_missing_or_unsafe_category_fields(self):
        from register import RegistrationError, register_category

        invalid_cards = []
        missing = dict(self.category)
        missing.pop("description")
        invalid_cards.append(missing)
        invalid_cards.extend(
            [
                dict(self.category, schema_version=True),
                dict(self.category, schema_version=1.0),
                dict(self.category, category_id="../escape"),
                dict(self.category, category_id="skill_learning"),
                dict(self.category, category_id="bad-"),
                dict(self.category, accent="rainbow"),
                dict(self.category, accent=["teal"]),
                dict(self.category, secret_note="do not publish"),
            ]
        )
        for card in invalid_cards:
            with self.subTest(card=card):
                with self.assertRaises(RegistrationError):
                    register_category(card, self.root)

    def test_registry_rejects_unknown_missing_or_unsafe_item_fields(self):
        from register import RegistrationError, register_category, register_item

        register_category(self.category, self.root)
        invalid_cards = []
        missing = dict(self.item)
        missing.pop("primary_url")
        invalid_cards.append(missing)
        invalid_cards.extend(
            [
                dict(self.item, schema_version=True),
                dict(self.item, item_id="../escape"),
                dict(self.item, item_id="bad-"),
                dict(self.item, category_id="bad-"),
                dict(self.item, primary_url="javascript:alert(1)"),
                dict(self.item, primary_url="https://:443/path"),
                dict(self.item, primary_url="https://exa mple.com/path"),
                dict(self.item, primary_url="https://./path"),
                dict(self.item, primary_url="https://-bad-.example/path"),
                dict(self.item, badges=["x"] * 5),
                dict(self.item, stats=[{"label": "x", "value": "y", "sub": "z"}] * 5),
                dict(self.item, extra_private_note="no"),
            ]
        )
        for card in invalid_cards:
            with self.subTest(card=card):
                with self.assertRaises(RegistrationError):
                    register_item(card, self.root)

    def test_dashboard_escapes_untrusted_text_and_preserves_safe_links(self):
        from register import register_category, register_item

        category = dict(self.category, title='<img src=x onerror="alert(1)">')
        item = dict(self.item, summary="<script>alert(1)</script>")
        register_category(category, self.root)
        register_item(item, self.root)

        root_html = (self.public_root / "dashboard.html").read_text()
        category_html = (
            self.public_root / "categories" / "skill-learning" / "index.html"
        ).read_text()
        self.assertNotIn("<img src=x", root_html)
        self.assertNotIn("<script>alert(1)</script>", category_html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", category_html)
        self.assertIn(self.item["primary_url"].replace("&", "&amp;"), category_html)

    def test_categories_sort_by_order_and_items_sort_by_actual_instant(self):
        from build_site import build_site

        finance = dict(
            self.category,
            category_id="investment-research",
            title="Investment Research",
            sort_order=10,
        )
        earlier = dict(self.item, published_at="2026-08-16T00:00:00+14:00", title="Earlier")
        later = dict(
            self.item,
            item_id="later-item",
            published_at="2026-08-15T23:00:00-12:00",
            title="Later",
        )
        rendered = build_site([self.category, finance], [earlier, later])

        self.assertLess(
            rendered.root_html.index("Investment Research"),
            rendered.root_html.index("Skill Learning"),
        )
        category_html = rendered.category_html["skill-learning"]
        self.assertLess(category_html.index(">Later<"), category_html.index(">Earlier<"))

    def test_builder_rejects_duplicate_item_identity(self):
        from build_site import build_site

        with self.assertRaises(ValueError):
            build_site(
                [self.category],
                [self.item, dict(self.item, summary="different")],
            )

    def test_active_public_release_is_unchanged_when_activation_fails(self):
        from unittest.mock import patch

        import register

        register.register_category(self.category, self.root)
        before = (self.public_root / "index.json").read_text()
        with patch("register._activate_release", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                register.register_item(self.item, self.root)

        self.assertEqual((self.public_root / "index.json").read_text(), before)
        self.assertFalse(any(self.public_root.glob("categories/*/items/*/card.json")))

    def test_missing_current_after_prior_release_fails_closed(self):
        from register import RegistrationError, register_category

        register_category(self.category, self.root)
        self.public_root.unlink()

        with self.assertRaises(RegistrationError):
            register_category(dict(self.category, category_id="other"), self.root)

    def test_missing_private_registry_inside_current_fails_closed(self):
        from register import RegistrationError, register_category

        register_category(self.category, self.root)
        registry_root = self.public_root / "_registry"
        for path in sorted(registry_root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        registry_root.rmdir()

        with self.assertRaises(RegistrationError):
            register_category(dict(self.category, category_id="other"), self.root)

    def test_emptied_private_registry_fails_closed_against_public_index(self):
        from register import RegistrationError, register_category

        register_category(self.category, self.root)
        for path in (self.public_root / "_registry").glob("**/*.json"):
            path.unlink()

        with self.assertRaises(RegistrationError):
            register_category(dict(self.category, category_id="other"), self.root)

    def test_missing_single_private_item_fails_closed_against_public_index(self):
        from register import RegistrationError, register_category, register_item

        register_category(self.category, self.root)
        register_item(self.item, self.root)
        private_item = next((self.public_root / "_registry" / "items").glob("*/*.json"))
        private_item.unlink()

        with self.assertRaises(RegistrationError):
            register_category(self.category, self.root)

    def test_count_preserving_private_item_substitution_fails_closed(self):
        from register import RegistrationError, register_category, register_item

        register_category(self.category, self.root)
        register_item(self.item, self.root)
        register_item(dict(self.item, item_id="second"), self.root)
        private_item = next(
            (self.public_root / "_registry" / "items" / "skill-learning").glob("*.json")
        )
        substituted = json.loads(private_item.read_text())
        substituted["item_id"] = "third"
        private_item.unlink()
        (private_item.parent / "third.json").write_text(json.dumps(substituted))

        with self.assertRaises(RegistrationError):
            register_category(self.category, self.root)

    def test_post_swap_directory_fsync_failure_does_not_report_false_failure(self):
        from unittest.mock import patch

        import register

        register.register_category(self.category, self.root)
        original_fsync = register.os.fsync

        def fail_directory_fsync(fd):
            if register.os.path.isdir(f"/proc/self/fd/{fd}"):
                raise OSError("simulated directory fsync failure")
            return original_fsync(fd)

        with patch("register.os.fsync", side_effect=fail_directory_fsync):
            register.register_item(self.item, self.root)

        self.assertEqual(
            json.loads((self.public_root / "index.json").read_text())["item_count"], 1
        )

    def test_post_swap_directory_open_failure_does_not_report_false_failure(self):
        from unittest.mock import patch

        import register

        register.register_category(self.category, self.root)
        original_open = register.os.open

        def fail_archive_directory_open(path, flags, *args, **kwargs):
            if Path(path) == self.root:
                raise OSError("simulated directory open failure")
            return original_open(path, flags, *args, **kwargs)

        with patch("register.os.open", side_effect=fail_archive_directory_open):
            register.register_item(self.item, self.root)

        self.assertEqual(
            json.loads((self.public_root / "index.json").read_text())["item_count"], 1
        )

    def test_post_swap_directory_close_failure_does_not_report_false_failure(self):
        from unittest.mock import patch

        import register

        register.register_category(self.category, self.root)
        with patch("register.os.close", side_effect=OSError("simulated close failure")):
            register.register_item(self.item, self.root)

        self.assertEqual(
            json.loads((self.public_root / "index.json").read_text())["item_count"], 1
        )

    def test_builder_refuses_to_mutate_active_public_release(self):
        from unittest.mock import patch

        import build_site

        release = self.root / ".releases" / "base"
        release.mkdir(parents=True)
        (self.root / "current").symlink_to(Path(".releases/base"))
        with patch("build_site.DEFAULT_ARCHIVE", self.root, create=True):
            with self.assertRaises(RuntimeError):
                build_site.write_public_site(release, [self.category], [])

    def test_builder_detects_active_release_for_custom_archive_without_global_patch(self):
        import build_site

        release = self.root / ".releases" / "base"
        release.mkdir(parents=True)
        (self.root / "current").symlink_to(Path(".releases/base"))

        with self.assertRaises(RuntimeError):
            build_site.write_public_site(release, [self.category], [])

    def test_builder_refuses_to_write_inside_active_release_descendant(self):
        import build_site

        release = self.root / ".releases" / "base"
        release.mkdir(parents=True)
        (self.root / "current").symlink_to(Path(".releases/base"))

        with self.assertRaises(RuntimeError):
            build_site.write_public_site(release / "nested", [self.category], [])

    def test_cli_subcommands_register_category_and_item(self):
        from register import main
        from unittest.mock import patch

        category_path = self.root / "category-input.json"
        item_path = self.root / "item-input.json"
        category_path.write_text(json.dumps(self.category))
        item_path.write_text(json.dumps(self.item))

        with redirect_stdout(io.StringIO()):
            with patch.object(
                sys,
                "argv",
                ["register.py", "category", "--card-json", str(category_path), "--root", str(self.root)],
            ):
                main()
            with patch.object(
                sys,
                "argv",
                ["register.py", "item", "--card-json", str(item_path), "--root", str(self.root)],
            ):
                main()

        self.assertEqual(
            json.loads((self.public_root / "index.json").read_text())["item_count"], 1
        )

    def test_cli_registers_category_from_central_catalog(self):
        from register import main
        from unittest.mock import patch

        catalog_root = self.root / "catalog"
        catalog_root.mkdir()
        (catalog_root / "skill-learning.json").write_text(
            json.dumps(self.category), encoding="utf-8"
        )

        with redirect_stdout(io.StringIO()):
            with patch.object(
                sys,
                "argv",
                [
                    "register.py",
                    "category",
                    "--category-id",
                    "skill-learning",
                    "--catalog-root",
                    str(catalog_root),
                    "--root",
                    str(self.root),
                ],
            ):
                main()

        category = json.loads(
            (self.public_root / "categories" / "skill-learning" / "category.json").read_text()
        )
        self.assertEqual(category, self.category)

    def test_all_shipped_catalog_categories_validate(self):
        from register import DEFAULT_CATEGORY_CATALOG, load_catalog_category

        paths = sorted(DEFAULT_CATEGORY_CATALOG.glob("*.json"))
        self.assertGreaterEqual(len(paths), 6)
        for path in paths:
            with self.subTest(category_id=path.stem):
                category = load_catalog_category(path.stem)
                self.assertEqual(category["category_id"], path.stem)


if __name__ == "__main__":
    unittest.main()
