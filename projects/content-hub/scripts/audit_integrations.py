#!/usr/bin/env python3
"""Audit declared Hermes Skill integrations with the unified Content Hub."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from registry_schema import ValidationError, validate_category  # noqa: E402

VALID_MODES = {"direct", "collection", "none"}
DIRECT_IDENTITIES = {"stable", "versioned"}
LEGACY_MARKERS = (
    "investment-research-registry",
    "register_report.py",
    "finance.ai.jingtao.fun",
    "finance hub",
    "finance-hub",
)


def _load_json(path: Path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _skill_paths(skills_root: Path, name: str) -> list[Path]:
    return sorted(
        path
        for path in Path(skills_root).glob("*/*/SKILL.md")
        if path.parent.name == name
    )


def _catalog_categories(category_catalog_root: Path) -> tuple[set[str], list[str]]:
    category_ids: set[str] = set()
    errors: list[str] = []
    root = Path(category_catalog_root)
    for path in sorted(root.glob("*.json")):
        if path.is_symlink():
            errors.append(f"category catalog may not contain symlinks: {path.name}")
            continue
        try:
            category = validate_category(_load_json(path))
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            errors.append(f"invalid category catalog entry {path.name}: {exc}")
            continue
        category_id = category["category_id"]
        if path.stem != category_id:
            errors.append(
                f"category catalog filename disagrees with category_id: {path.name}"
            )
        if category_id in category_ids:
            errors.append(f"duplicate category_id in catalog: {category_id}")
        category_ids.add(category_id)
    if not category_ids:
        errors.append("category catalog contains no valid categories")
    return category_ids, errors


def _expected_contract(policy: dict) -> str:
    mode = policy["mode"]
    if mode == "none":
        return "**Content Hub contract:** none"
    return (
        f"**Content Hub contract:** {mode} · "
        f"category={policy['category_id']} · identity={policy['identity_policy']}"
    )


def audit_integrations(
    config_path: Path, skills_root: Path, category_catalog_root: Path
) -> list[str]:
    errors: list[str] = []
    try:
        config = _load_json(config_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"integration catalog cannot be loaded: {exc}"]
    if not isinstance(config, dict):
        return ["integration catalog must be an object"]
    if set(config) != {"schema_version", "skills"}:
        errors.append("integration catalog has unknown or missing top-level fields")
    if type(config.get("schema_version")) is not int or config.get("schema_version") != 1:
        errors.append("integration catalog schema_version must be integer 1")
    integrations = config.get("skills")
    if not isinstance(integrations, dict):
        errors.append("integration catalog skills must be an object")
        return errors

    contract_marked_skills: set[str] = set()
    for installed_skill in Path(skills_root).glob("*/*/SKILL.md"):
        try:
            installed_text = installed_skill.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "**Content Hub contract:**" in installed_text:
            contract_marked_skills.add(installed_skill.parent.name)
    omitted_skills = contract_marked_skills - set(integrations)
    for omitted_skill in sorted(omitted_skills):
        errors.append(
            f"{omitted_skill}: contract-marked Skill omitted from integration catalog"
        )

    known_categories, category_errors = _catalog_categories(category_catalog_root)
    errors.extend(category_errors)

    for name, policy in sorted(integrations.items()):
        paths = _skill_paths(skills_root, name)
        if len(paths) != 1:
            errors.append(
                f"{name}: expected exactly one installed SKILL.md, found {len(paths)}"
            )
            continue
        skill_path = paths[0]
        text = skill_path.read_text(encoding="utf-8")
        description_match = re.search(r"^description:\s*[\"']?([^\n\"']+)", text, re.M)
        if not description_match or len(description_match.group(1).strip()) > 60:
            errors.append(f"{name}: description must be present and at most 60 characters")
        if not isinstance(policy, dict):
            errors.append(f"{name}: integration policy must be an object")
            continue
        mode = policy.get("mode")
        if mode not in VALID_MODES:
            errors.append(f"{name}: unsupported integration mode {mode!r}")
            continue

        expected_fields = (
            {"mode", "reason"}
            if mode == "none"
            else {"mode", "category_id", "identity_policy"}
        )
        if set(policy) != expected_fields:
            errors.append(
                f"{name}: {mode} policy fields must be exactly {sorted(expected_fields)}"
            )

        for candidate in skill_path.parent.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                candidate_bytes = candidate.read_bytes()
            except OSError as exc:
                errors.append(f"{name}: cannot read support file {candidate.name}: {exc}")
                continue
            if b"\x00" in candidate_bytes:
                continue
            try:
                candidate_text = candidate_bytes.decode("utf-8")
            except UnicodeDecodeError:
                relative = candidate.relative_to(skill_path.parent)
                errors.append(
                    f"{name}: nonbinary support file is not valid UTF-8: {relative}"
                )
                continue
            candidate_text_lower = candidate_text.lower()
            for marker in LEGACY_MARKERS:
                if marker in candidate_text_lower:
                    relative = candidate.relative_to(skill_path.parent)
                    errors.append(
                        f"{name}: legacy registry marker remains in {relative}: {marker}"
                    )

        expected_contract = _expected_contract(policy)
        contract_lines = re.findall(r"^\*\*Content Hub contract:\*\* .+$", text, re.M)
        if contract_lines != [expected_contract]:
            errors.append(
                f"{name}: expected exactly one contract marker: {expected_contract}"
            )

        semantic_text = re.sub(r"[`*_]", "", text).lower()
        semantic_sentences = [
            sentence.strip()
            for sentence in re.split(r"[.;。；\n]+", semantic_text)
            if sentence.strip()
        ]

        if mode == "none":
            if not isinstance(policy.get("reason"), str) or not policy["reason"].strip():
                errors.append(f"{name}: none policy requires a non-empty reason")
            if "does not register" not in semantic_text:
                errors.append(
                    f"{name}: none policy must state that this Skill does not register"
                )
            if re.search(
                r"(?:register (?:reports?|artifacts?|items?) directly|register directly|"
                r"must register|this skill registers)",
                semantic_text,
            ):
                errors.append(
                    f"{name}: none policy contains a conflicting direct registration instruction"
                )
            if "load content-hub-registry" in semantic_text:
                errors.append(
                    f"{name}: none policy may not load content-hub-registry directly"
                )
            positive_registration_sentence = any(
                re.search(r"\bregister\b", sentence)
                and not re.search(r"\b(do not|does not|don't|never|not)\b", sentence)
                for sentence in semantic_sentences
            )
            if positive_registration_sentence:
                errors.append(
                    f"{name}: none policy contains a conflicting direct registration instruction"
                )
            continue

        category_id = policy.get("category_id")
        if category_id not in known_categories:
            errors.append(f"{name}: unknown category_id {category_id!r}")
        identity = policy.get("identity_policy")
        if mode == "direct" and identity not in DIRECT_IDENTITIES:
            errors.append(f"{name}: direct identity_policy must be stable or versioned")
        direct_collection_workflow = any(
            "collection" in sentence
            and any(
                action in sentence
                for action in ("register", "maintain", "publish", "update")
            )
            for sentence in semantic_sentences
        )
        if mode == "direct" and direct_collection_workflow:
            errors.append(f"{name}: direct policy contains a collection workflow")
        if mode == "collection" and identity != "collection":
            errors.append(f"{name}: collection identity_policy must be collection")
        if mode == "collection" and "do not register every" not in semantic_text:
            errors.append(
                f"{name}: collection policy must say do not register every item"
            )
        positive_per_item_registration = any(
            "register" in sentence
            and re.search(r"\b(each|every|per[ -]?item)\b", sentence)
            and not re.search(r"\b(do not|don't|never|not)\b", sentence)
            for sentence in semantic_sentences
        )
        if mode == "collection" and positive_per_item_registration:
            errors.append(
                f"{name}: collection policy contains a positive per-item registration instruction"
            )
        if "load `content-hub-registry`" not in text.lower():
            errors.append(
                f"{name}: {mode} integration must explicitly load content-hub-registry"
            )
    return errors


def main() -> int:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "catalog" / "integrations.json",
    )
    parser.add_argument("--skills-root", type=Path, default=hermes_home / "skills")
    parser.add_argument(
        "--category-catalog-root",
        type=Path,
        default=PROJECT_ROOT / "catalog" / "categories",
    )
    args = parser.parse_args()
    errors = audit_integrations(
        args.config, args.skills_root, args.category_catalog_root
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Content Hub Skill integrations: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
