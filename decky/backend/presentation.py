"""Pure descriptions copied from Portal's frontend workflow dependencies.

Keep rollback_cancel_target in parity with maintenance/rollback_cancel.py:target
and its identity helpers. This module does not query or modify the system. Portal
still validates the confirmation token again before executing a request.
"""
import hashlib
import json
import re

from . import messages as text

TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _unavailable(reason):
    return {"available": False, "token": "", "reason": reason[:1024],
            "version": text.UNKNOWN, "identity": ""}


def _text(value, limit, *, empty=False):
    return (isinstance(value, str) and len(value) <= limit
            and (empty or bool(value.strip()))
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _encoded(identities):
    return json.dumps(identities, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _identities(data):
    if not isinstance(data, dict) or "deployments" not in data:
        raise ValueError(text.DEPLOYMENT_UNAVAILABLE)
    value = data.get("transaction")
    if value is not None and value is not False and value != "" and value != [] and value != {}:
        raise ValueError(text.TRANSACTION_ACTIVE)
    entries = data["deployments"]
    if not isinstance(entries, list) or not entries or len(entries) > 64:
        raise ValueError(text.DEPLOYMENT_LIST_INVALID)
    identities, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(text.DEPLOYMENT_LIST_MALFORMED)
        deployment_id, checksum = entry.get("id"), entry.get("checksum")
        osname, reference = entry.get("osname"), entry.get("container-image-reference", "")
        if (not _text(deployment_id, 256) or deployment_id != deployment_id.strip()
                or not isinstance(checksum, str) or not CHECKSUM_PATTERN.fullmatch(checksum)
                or not _text(osname, 256) or osname != osname.strip()
                or not _text(reference, 2048, empty=True)
                or not isinstance(entry.get("booted"), bool)
                or not isinstance(entry.get("pinned"), bool)
                or not isinstance(entry.get("staged", False), bool)):
            raise ValueError(text.DEPLOYMENT_IDENTITY_INVALID)
        if deployment_id in seen:
            raise ValueError(text.DEPLOYMENT_AMBIGUOUS)
        seen.add(deployment_id)
        if entry.get("staged", False):
            raise ValueError(text.STAGED_DEPLOYMENT)
        identities.append({"id": deployment_id, "checksum": checksum.lower(),
                           "osname": osname, "container-image-reference": reference,
                           "booted": entry["booted"], "pinned": entry["pinned"], "staged": False})
    if sum(entry["booted"] for entry in identities) != 1:
        raise ValueError(text.BOOTED_INVALID)
    if len(_encoded(identities)) > 128 * 1024:
        raise ValueError(text.IDENTITY_TOO_LARGE)
    return identities


def rollback_cancel_target(data):
    try:
        identities = _identities(data)
        booted_index = next(index for index, item in enumerate(identities) if item["booted"])
        if booted_index == 0:
            return _unavailable(text.NO_ROLLBACK)
        if identities[0]["osname"] != identities[booted_index]["osname"]:
            return _unavailable(text.DIFFERENT_STATEROOTS)
    except ValueError as error:
        return _unavailable(str(error))
    version = data["deployments"][booted_index].get("version")
    if isinstance(version, str):
        version = "".join(char for char in version if ord(char) >= 32 and ord(char) != 127).strip()[:1024]
    else:
        version = ""
    return {"available": True, "token": hashlib.sha256(_encoded(identities)).hexdigest(),
            "reason": "", "version": version or text.UNKNOWN,
            "identity": identities[booted_index]["checksum"]}
