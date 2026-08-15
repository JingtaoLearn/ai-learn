"""Validation contracts for the generic two-level content hub."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from urllib.parse import urlsplit


class ValidationError(ValueError):
    """Raised when public registry metadata violates its schema."""


CATEGORY_REQUIRED = {
    "schema_version",
    "category_id",
    "title",
    "subtitle",
    "description",
    "icon",
    "accent",
    "sort_order",
    "item_label",
    "source_skill",
}
ITEM_REQUIRED = {
    "schema_version",
    "category_id",
    "item_id",
    "title",
    "subtitle",
    "published_at",
    "primary_url",
    "source_url",
    "summary",
    "badges",
    "stats",
    "highlights",
    "tags",
    "source_skill",
}
CATEGORY_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
ITEM_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
ACCENTS = {"terracotta", "teal", "blue", "purple", "amber", "green"}


def _validate_exact_fields(payload: object, required: set[str], kind: str) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError(f"{kind} card must be a JSON object")
    missing = required - set(payload)
    unknown = set(payload) - required
    if missing:
        raise ValidationError(f"missing {kind} field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ValidationError(f"unknown {kind} field(s): {', '.join(sorted(unknown))}")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValidationError("schema_version must be the integer 1")
    return dict(payload)


def _text(name: str, value: object, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValidationError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValidationError(f"{name} must be <= {maximum} characters")
    if any(ord(char) < 32 and char not in "\t\n" for char in value):
        raise ValidationError(f"{name} contains control characters")
    return value


def _https_url(name: str, value: object) -> str:
    value = _text(name, value, maximum=2048)
    if "\\" in value or any(ord(char) < 32 for char in value):
        raise ValidationError(f"{name} contains unsafe characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(f"{name} is not a valid URL") from exc
    if parsed.scheme != "https" or not parsed.netloc or not hostname:
        raise ValidationError(f"{name} must be an absolute https URL with a hostname")
    if parsed.username or parsed.password:
        raise ValidationError(f"{name} must not contain credentials")
    if port is not None and not (1 <= port <= 65535):
        raise ValidationError(f"{name} contains an invalid port")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValidationError(f"{name} contains an invalid hostname") from exc
        labels = ascii_hostname.split(".")
        if (
            len(ascii_hostname) > 253
            or any(
                not label
                or len(label) > 63
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                for label in labels
            )
        ):
            raise ValidationError(f"{name} contains an invalid hostname")
    return value


def parse_published_at(value: object) -> datetime:
    value = _text("published_at", value, maximum=64)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("published_at must be valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError("published_at must include a timezone offset")
    return parsed


def validate_category(payload: object) -> dict:
    card = _validate_exact_fields(payload, CATEGORY_REQUIRED, "category")
    if not isinstance(card["category_id"], str) or not CATEGORY_ID.fullmatch(card["category_id"]):
        raise ValidationError("category_id must match [a-z0-9-]+ and be <=64 characters")
    for name, maximum in (
        ("title", 120),
        ("subtitle", 160),
        ("description", 500),
        ("icon", 16),
        ("item_label", 40),
        ("source_skill", 120),
    ):
        _text(name, card[name], maximum=maximum)
    if not isinstance(card["accent"], str) or card["accent"] not in ACCENTS:
        raise ValidationError(f"accent must be one of {sorted(ACCENTS)}")
    if type(card["sort_order"]) is not int or not (-1000 <= card["sort_order"] <= 1000):
        raise ValidationError("sort_order must be an integer between -1000 and 1000")
    return card


def _validate_string_list(name: str, value: object, *, maximum_items: int, maximum_length: int) -> None:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValidationError(f"{name} must be an array with at most {maximum_items} items")
    for item in value:
        _text(f"{name} item", item, maximum=maximum_length)


def validate_item(payload: object) -> dict:
    card = _validate_exact_fields(payload, ITEM_REQUIRED, "item")
    if not isinstance(card["category_id"], str) or not CATEGORY_ID.fullmatch(card["category_id"]):
        raise ValidationError("category_id must match [a-z0-9-]+ and be <=64 characters")
    if not isinstance(card["item_id"], str) or not ITEM_ID.fullmatch(card["item_id"]):
        raise ValidationError("item_id must match [a-z0-9-]+ and be <=128 characters")
    for name, maximum in (
        ("title", 180),
        ("subtitle", 300),
        ("summary", 800),
        ("source_skill", 120),
    ):
        _text(name, card[name], maximum=maximum)
    parse_published_at(card["published_at"])
    _https_url("primary_url", card["primary_url"])
    _text("source_url", card["source_url"], maximum=2048, allow_empty=True)
    if card["source_url"]:
        _https_url("source_url", card["source_url"])
    _validate_string_list("badges", card["badges"], maximum_items=3, maximum_length=40)
    _validate_string_list("highlights", card["highlights"], maximum_items=5, maximum_length=200)
    _validate_string_list("tags", card["tags"], maximum_items=8, maximum_length=40)
    stats = card["stats"]
    if not isinstance(stats, list) or len(stats) > 3:
        raise ValidationError("stats must be an array with at most 3 items")
    for stat in stats:
        if not isinstance(stat, dict) or set(stat) != {"label", "value", "sub"}:
            raise ValidationError("each stat must contain exactly label, value, and sub")
        _text("stat label", stat["label"], maximum=40)
        _text("stat value", stat["value"], maximum=80)
        _text("stat sub", stat["sub"], maximum=120, allow_empty=True)
    return card
