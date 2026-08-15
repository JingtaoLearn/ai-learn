#!/usr/bin/env python3
"""Register categories and items in the generic two-level content hub."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from build_site import write_public_site
from registry_schema import ValidationError, validate_category, validate_item

DEFAULT_ARCHIVE = Path.home() / "content-hub"
RegistrationError = ValidationError


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_current(archive: Path) -> tuple[list[dict], list[dict]]:
    current = archive / "current"
    if not current.is_dir():
        releases = archive / ".releases"
        if releases.is_dir() and any(path.is_dir() for path in releases.iterdir()):
            raise RegistrationError(
                "current release pointer is missing or broken; refusing to rebuild from empty state"
            )
        return [], []
    categories_root = current / "_registry" / "categories"
    items_root = current / "_registry" / "items"
    if not categories_root.is_dir() or not items_root.is_dir():
        raise RegistrationError(
            "active release is missing private registry state; refusing to rebuild"
        )
    categories = [
        validate_category(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(categories_root.glob("*.json"))
    ]
    items = [
        validate_item(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(items_root.glob("*/*.json"))
    ]
    try:
        public_index = json.loads((current / "index.json").read_text(encoding="utf-8"))
        public_category_ids = {
            entry["category_id"] for entry in public_index["categories"]
        }
        public_categories = [
            validate_category(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted((current / "categories").glob("*/category.json"))
        ]
        public_items = [
            validate_item(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted((current / "categories").glob("*/items/*/card.json"))
        ]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RegistrationError(
            "active release public index is missing or malformed"
        ) from exc
    private_category_ids = {entry["category_id"] for entry in categories}
    private_categories_by_id = {entry["category_id"]: entry for entry in categories}
    public_categories_by_id = {entry["category_id"]: entry for entry in public_categories}
    private_items_by_id = {
        (entry["category_id"], entry["item_id"]): entry for entry in items
    }
    public_items_by_id = {
        (entry["category_id"], entry["item_id"]): entry for entry in public_items
    }
    if (
        public_index.get("category_count") != len(categories)
        or public_index.get("item_count") != len(items)
        or len(public_categories) != len(categories)
        or len(public_items) != len(items)
        or public_category_ids != private_category_ids
        or private_categories_by_id != public_categories_by_id
        or private_items_by_id != public_items_by_id
    ):
        raise RegistrationError(
            "private registry state disagrees with the active public index"
        )
    return categories, items


def _activate_release(archive: Path, release: Path) -> None:
    relative_target = release.relative_to(archive)
    temporary_link = archive / f".current-{uuid.uuid4().hex}.tmp"
    current = archive / "current"
    try:
        os.symlink(relative_target, temporary_link)
        os.replace(temporary_link, current)
        try:
            directory_fd = os.open(archive, os.O_RDONLY)
        except OSError:
            # The atomic swap has already committed. Directory durability is
            # best-effort on filesystems that reject opening directories.
            return
        try:
            try:
                os.fsync(directory_fd)
            except OSError:
                # The public swap has already committed. Some filesystems do
                # not support directory fsync; reporting failure now would be
                # false because readers already see the complete new release.
                pass
        finally:
            try:
                os.close(directory_fd)
            except OSError:
                # Closing the directory descriptor happens after the atomic
                # public swap; never turn a committed update into a false
                # failure report.
                pass
    finally:
        try:
            if temporary_link.is_symlink():
                temporary_link.unlink()
        except OSError:
            # Cleanup is best-effort and must not mask either the activation
            # result or an earlier pre-swap failure.
            pass


def _commit_release(archive: Path, categories: list[dict], items: list[dict]) -> None:
    releases = archive / ".releases"
    releases.mkdir(parents=True, exist_ok=True)
    staging = archive / f".staging-{uuid.uuid4().hex}"
    release = releases / uuid.uuid4().hex
    try:
        staging.mkdir()
        (staging / "_registry" / "categories").mkdir(parents=True)
        (staging / "_registry" / "items").mkdir(parents=True)
        for category in categories:
            _atomic_json(
                staging / "_registry" / "categories" / f"{category['category_id']}.json",
                category,
            )
        for item in items:
            _atomic_json(
                staging
                / "_registry"
                / "items"
                / item["category_id"]
                / f"{item['item_id']}.json",
                item,
            )
        write_public_site(staging, categories, items)
        os.replace(staging, release)
        _activate_release(archive, release)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _with_lock(root: Path, mutator) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".registry.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            categories, items = _load_current(root)
            categories, items = mutator(categories, items)
            _commit_release(root, categories, items)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def register_category(payload: object, root: Path = DEFAULT_ARCHIVE) -> Path:
    category = validate_category(payload)
    archive = Path(root).expanduser().resolve()

    def mutate(categories: list[dict], items: list[dict]):
        categories = [entry for entry in categories if entry["category_id"] != category["category_id"]]
        categories.append(category)
        return categories, items

    _with_lock(archive, mutate)
    return archive / "current" / "categories" / category["category_id"] / "category.json"


def register_item(payload: object, root: Path = DEFAULT_ARCHIVE) -> Path:
    item = validate_item(payload)
    archive = Path(root).expanduser().resolve()

    def mutate(categories: list[dict], items: list[dict]):
        if item["category_id"] not in {entry["category_id"] for entry in categories}:
            raise RegistrationError(
                f"category must be registered first: {item['category_id']}"
            )
        items = [
            entry
            for entry in items
            if not (
                entry["category_id"] == item["category_id"]
                and entry["item_id"] == item["item_id"]
            )
        ]
        items.append(item)
        return categories, items

    _with_lock(archive, mutate)
    return (
        archive
        / "current"
        / "categories"
        / item["category_id"]
        / "items"
        / item["item_id"]
        / "card.json"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("category", "item"):
        child = subparsers.add_parser(command)
        child.add_argument("--card-json", required=True, type=Path)
        child.add_argument("--root", type=Path, default=DEFAULT_ARCHIVE)
    args = parser.parse_args()
    payload = json.loads(args.card_json.read_text(encoding="utf-8"))
    if args.command == "category":
        destination = register_category(payload, args.root)
    else:
        destination = register_item(payload, args.root)
    print(f"Registered {destination}")
    print(f"Hub {args.root / 'current' / 'dashboard.html'}")


if __name__ == "__main__":
    main()
