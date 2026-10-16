"""Pure summaries of already-read rpm-ostree and bootc status data."""

from __future__ import annotations
from bazzite_portal.core.i18n import N_

import json
from typing import Any


MAX_ENTRIES = 32
MAX_TEXT_LENGTH = 1024
UNKNOWN_TEXT = N_("Unknown")


def _text(value: Any) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return UNKNOWN_TEXT
    cleaned = "".join(char for char in str(value) if ord(char) >= 32 and ord(char) != 127).strip()
    return cleaned[:MAX_TEXT_LENGTH] or UNKNOWN_TEXT


def _transaction(value: Any) -> str:
    if isinstance(value, str):
        cleaned = "".join(char for char in value if ord(char) >= 32 and ord(char) != 127).strip()
        return cleaned[:MAX_TEXT_LENGTH]
    if isinstance(value, list) and value:
        try:
            rendered = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
        except (TypeError, ValueError):
            rendered = " ".join(str(part) for part in value)
        return "".join(char for char in rendered if ord(char) >= 32 and ord(char) != 127).strip()[:MAX_TEXT_LENGTH]
    return ""


def _unknown(engine: str = "", error: str = "Deployment status is unavailable", transaction: str = "") -> dict[str, Any]:
    return {
        "known": False,
        "error": error[:MAX_TEXT_LENGTH],
        "engine": engine,
        "entries": [],
        "next_boot_same": False,
        "transaction": transaction[:MAX_TEXT_LENGTH],
    }


def _deployment_entry(
    item: dict[str, Any], role: str, *, staged: bool = False,
) -> dict[str, Any]:
    image = item.get("container-image-reference") or item.get("image") or item.get("image-reference")
    return {
        "role": role,
        "version": _text(item.get("version")),
        "image": _text(image),
        "identity": _text(item.get("checksum") or item.get("id")),
        "pinned": item.get("pinned") is True,
        "staged": staged or item.get("staged") is True,
    }


def _summarize_rpm_ostree(data: dict[str, Any]) -> dict[str, Any]:
    transaction = _transaction(data.get("transaction"))
    deployments = data.get("deployments")
    if not isinstance(deployments, list) or not deployments:
        return _unknown("rpm-ostree", "Deployment list is missing or empty", transaction)
    if any(not isinstance(item, dict) or not isinstance(item.get("booted"), bool) for item in deployments):
        return _unknown("rpm-ostree", "Deployment list is malformed", transaction)
    booted_indexes = [index for index, item in enumerate(deployments) if item["booted"]]
    if len(booted_indexes) != 1:
        return _unknown("rpm-ostree", "Expected exactly one running deployment", transaction)
    if booted_indexes[0] >= MAX_ENTRIES:
        return _unknown("rpm-ostree", "Running deployment is outside the display limit", transaction)

    next_boot_same = deployments[0]["booted"]
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(deployments[:MAX_ENTRIES]):
        if item["booted"]:
            role = "Running now"
        elif index == 0:
            role = "Next restart"
        else:
            role = "Saved deployment"
        entries.append(_deployment_entry(item, role))
    return {
        "known": True,
        "error": "",
        "engine": "rpm-ostree",
        "entries": entries,
        "next_boot_same": next_boot_same,
        "transaction": transaction,
    }


def _bootc_part(item: Any, role: str, *, staged: bool = False) -> dict[str, Any] | None:
    if item is None:
        return None
    if not isinstance(item, dict):
        raise ValueError("bootc deployment details are malformed")
    image_data = item.get("image")
    if not isinstance(image_data, dict):
        raise ValueError("bootc image details are missing")
    image_reference = image_data.get("image")
    if isinstance(image_reference, dict):
        image_reference = image_reference.get("image")
    ostree_data = item.get("ostree")
    if ostree_data is not None and not isinstance(ostree_data, dict):
        raise ValueError("bootc ostree details are malformed")
    identity = image_data.get("imageDigest") or item.get("imageDigest")
    if not identity and isinstance(ostree_data, dict):
        identity = ostree_data.get("checksum")
    return {
        "role": role,
        "version": _text(image_data.get("version") or item.get("version")),
        "image": _text(image_reference),
        "identity": _text(identity),
        "pinned": item.get("pinned") is True,
        "staged": staged,
    }


def _summarize_bootc(data: dict[str, Any]) -> dict[str, Any]:
    # bootc status --json nests deployment states under its status object.
    api_version = data.get("apiVersion")
    if api_version is not None and (
        not isinstance(api_version, str)
        or api_version not in {"org.containers.bootc/v1alpha1", "org.containers.bootc/v1"}
    ):
        return _unknown("bootc", "Unsupported bootc status API version")
    status = data.get("status")
    if not isinstance(status, dict) or not all(key in status for key in ("booted", "staged", "rollback")):
        return _unknown("bootc", "Bootc deployment status is incomplete")
    rollback_queued = status.get("rollbackQueued", False)
    if not isinstance(rollback_queued, bool):
        return _unknown("bootc", "Bootc rollback state is malformed")
    if rollback_queued and status["staged"] is not None:
        return _unknown("bootc", "Bootc reports conflicting staged and rollback deployments")
    if rollback_queued and status["rollback"] is None:
        return _unknown("bootc", "Bootc queued a rollback without a rollback deployment")
    try:
        booted = _bootc_part(status["booted"], "Running now")
        if booted is None:
            raise ValueError("bootc running deployment is missing")
        staged = _bootc_part(status["staged"], "Next restart", staged=True)
        rollback_role = "Next restart" if rollback_queued else "Saved deployment"
        rollback = _bootc_part(status["rollback"], rollback_role)
    except ValueError as exc:
        return _unknown("bootc", str(exc))
    entries = [booted]
    if staged is not None:
        entries.append(staged)
    if rollback is not None:
        entries.append(rollback)
    return {
        "known": True,
        "error": "",
        "engine": "bootc",
        "entries": entries[:MAX_ENTRIES],
        "next_boot_same": staged is None and not rollback_queued,
        "transaction": _transaction(data.get("transaction")),
    }


def summarize_deployments(data: Any) -> dict[str, Any]:
    """Summarize structured status without executing commands or mutating input."""
    if not isinstance(data, dict):
        return _unknown(error="Deployment status is not an object")
    if "deployments" in data:
        return _summarize_rpm_ostree(data)
    if "status" in data:
        return _summarize_bootc(data)
    return _unknown(error="Unsupported deployment status format")
