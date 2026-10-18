"""Bounded Bazzite image and version selection for RPM-OSTree rebases."""

from __future__ import annotations
from bazzite_portal.core.i18n import C_, N_

import hashlib
import importlib.resources
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable

from bazzite_portal.maintenance import cancel_pending
from bazzite_portal.maintenance import channel_switch

TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_STATUS_BYTES = 2 * 1024 * 1024
MAX_TAGS_BYTES = cancel_pending.MAX_STATUS_BYTES
MAX_DEPLOYMENTS = 64
MAX_TAGS = 10000
MAX_TAG_BYTES = 64
_IMAGE_NAME_RE = re.compile(r"^bazzite(?:-[a-z0-9]+)*$")
_CATALOG_NAMESPACE = "ghcr.io/ublue-os"
REGISTRY = _CATALOG_NAMESPACE


def _load_image_catalog() -> tuple[tuple[str, ...], dict[str, str]]:
    """Load the strictly validated, package-owned image catalog."""
    raw = importlib.resources.files(__package__).joinpath("rebase_images.json").read_bytes()
    if len(raw) > 64 * 1024:
        raise RuntimeError("The bundled Bazzite image catalog is too large.")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError("The bundled Bazzite image catalog is malformed.") from error
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "namespace", "images"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["namespace"] != _CATALOG_NAMESPACE
            or not isinstance(payload["images"], list) or not payload["images"]
            or len(payload["images"]) > 128):
        raise RuntimeError("The bundled Bazzite image catalog has an unsupported schema.")
    images: dict[str, str] = {}
    for entry in payload["images"]:
        if (not isinstance(entry, dict) or set(entry) != {"name", "desktop_environment"}
                or not isinstance(entry["name"], str) or not _IMAGE_NAME_RE.fullmatch(entry["name"])
                or not isinstance(entry["desktop_environment"], str)
                or entry["desktop_environment"] not in {"kde", "gnome"}
                or entry["desktop_environment"] != ("gnome" if "gnome" in entry["name"] else "kde")
                or entry["name"] in images):
            raise RuntimeError("The bundled Bazzite image catalog contains an invalid entry.")
        images[entry["name"]] = entry["desktop_environment"]
    return tuple(images), images


SUPPORTED_IMAGES, _IMAGE_DESKTOPS = _load_image_catalog()
_IMAGE_RE = re.compile(r"^(?:" + "|".join(re.escape(value) for value in SUPPORTED_IMAGES) + r")$")
_TAG_RE = re.compile(
    r"^(?:stable|testing|unstable|latest|\d{2,3}(?:\.\d+){0,3}|"
    r"(?:stable|testing|unstable)-\d{2,3}(?:\.\d+){0,3})$"
)
INSTANCE_PATTERN = re.compile(
    r"^([0-9a-f]{64})-(" + "|".join(re.escape(value) for value in SUPPORTED_IMAGES)
    + r")-((?:stable|testing|unstable|latest|\d{2,3}|"
      r"\d{2,3}(?:\.\d+){1,3}|"
      r"(?:stable|testing|unstable)-\d{2,3}(?:\.\d+){0,3}))$"
)
_REFERENCE_RE = re.compile(
    r"^(?:(?:ostree-image-signed:docker://)|ostree-unverified-registry:)"
    r"" + re.escape(_CATALOG_NAMESPACE) + r"/(?P<image>" + _IMAGE_RE.pattern[1:-1] + r"):"
    r"(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]{0,63})$"
)


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason[:1024], "token": "", "identity": "",
            "current_image": "", "current_tag": "", "image": "", "tag": "",
            "target_reference": "", "desktop_environment_change": False}


def _identity(entry: dict[str, Any]) -> dict[str, str] | None:
    deployment_id, checksum = entry.get("id"), entry.get("checksum")
    osname, reference = entry.get("osname"), entry.get("container-image-reference")
    if (not isinstance(deployment_id, str) or not deployment_id.strip() or len(deployment_id) > 256
            or not isinstance(checksum, str) or not CHECKSUM_PATTERN.fullmatch(checksum)
            or not isinstance(osname, str) or not osname.strip() or len(osname) > 128
            or not isinstance(reference, str) or len(reference) > 1024
            or any(ord(char) < 32 or ord(char) == 127
                   for value in (deployment_id, osname, reference) for char in value)):
        return None
    return {"id": deployment_id.strip(), "checksum": checksum.lower(), "osname": osname,
            "reference": reference}


def _snapshot(data: Any) -> tuple[dict[str, Any], dict[str, str], str, str] | dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("deployments"), list):
        return _unavailable("RPM-OSTree deployment status is unavailable.")
    deployments = data["deployments"]
    if not deployments or len(deployments) > MAX_DEPLOYMENTS:
        return _unavailable("RPM-OSTree deployment list is empty or too large.")
    if any(not isinstance(item, dict) or not isinstance(item.get("booted"), bool) for item in deployments):
        return _unavailable("RPM-OSTree deployment list is malformed.")
    if sum(item["booted"] for item in deployments) != 1:
        return _unavailable("The running deployment could not be identified safely.")
    if data.get("transaction") not in (None, False, "", [], {}):
        return _unavailable(C_('An {rpm_ostree} transaction is active.', rpm_ostree='RPM-OSTree'))
    if deployments[0].get("booted") is not True or deployments[0].get("staged") is True:
        return _unavailable(N_("Resolve the pending deployment before changing images."))
    running = next(item for item in deployments if item["booted"])
    identity = _identity(running)
    if identity is None:
        return _unavailable("The running deployment identity is incomplete.")
    match = _REFERENCE_RE.fullmatch(identity["reference"])
    if match is None or not _valid_tag(match.group("tag")):
        return _unavailable(C_('Only supported official {bazzite} image references can be changed.', bazzite='Bazzite'))
    return running, identity, match.group("image"), match.group("tag")


def _request_token(identity: dict[str, str], image: str, tag: str) -> str:
    raw = json.dumps({"identity": identity, "image": image, "tag": tag}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _desktop_environment(image: str) -> str:
    return _IMAGE_DESKTOPS[image]


def _valid_tag(tag: Any) -> bool:
    return (isinstance(tag, str) and len(tag) <= MAX_TAG_BYTES and tag.isascii()
            and _TAG_RE.fullmatch(tag) is not None)


def _list_tags(image: str, run_command: Callable[..., str]) -> list[str]:
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        raise ValueError("Unsupported Bazzite image variant.")
    try:
        output = run_command(["skopeo", "list-tags", f"docker://{REGISTRY}/{image}"], timeout=30)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Could not query available versions for {image}.") from error
    if not isinstance(output, str) or len(output.encode("utf-8", "replace")) > MAX_TAGS_BYTES:
        raise RuntimeError("The image version list is malformed or too large.")
    try:
        payload = json.loads(output)
    except (TypeError, ValueError) as error:
        raise RuntimeError("The image version list is malformed.") from error
    tags = payload.get("Tags") if isinstance(payload, dict) else None
    if not isinstance(tags, list) or len(tags) > MAX_TAGS or any(not isinstance(tag, str) for tag in tags):
        raise RuntimeError("The image version list is malformed or too large.")
    return tags


def images(run_command: Callable[..., str] = cancel_pending._command, *,
           show_unsupported: bool = False) -> dict[str, Any]:
    """List the bundled image catalog, filtered to the running desktop by default."""
    snapshot = _snapshot(_read_status(run_command))
    if isinstance(snapshot, dict):
        return {"available": False, "reason": snapshot["reason"], "images": []}
    desktop = _desktop_environment(snapshot[2])
    available = [{"image": image, "desktop_environment": _desktop_environment(image)}
                 for image in SUPPORTED_IMAGES
                 if show_unsupported or _desktop_environment(image) == desktop]
    return {"available": bool(available), "reason": "" if available else C_('No official {bazzite} images are available.', bazzite='Bazzite'),
            "images": available}


def versions(image: str, track: str = "all",
             run_command: Callable[..., str] = cancel_pending._command) -> dict[str, Any]:
    """List selectable tags for one catalog-supported image and track."""
    if track not in {"stable", "testing", "unstable", "all"}:
        raise ValueError("Unsupported Bazzite image track.")
    tags = _list_tags(image, run_command)
    selected = []
    for tag in tags:
        if not _valid_tag(tag):
            continue
        if track != "all" and tag != track and not tag.startswith(track + "-"):
            continue
        selected.append(tag)
    # Show moving tracks first, then group numeric releases by track with the
    # newest release first. Keep the moving-track choices visible.
    selected = sorted(set(selected), key=_version_sort_key)
    return {"available": bool(selected),
            "reason": "" if selected else C_("No {track} versions are available for {image}.", track=track, image=image),
            "image": image, "track": track, "versions": selected}


def _version_sort_key(tag: str):
    moving_order = {"stable": 0, "testing": 1, "unstable": 2, "latest": 3}
    if tag in moving_order:
        return (0, moving_order[tag], ())
    match = re.fullmatch(r"(?:(stable|testing|unstable)-)?(\d{2,3}(?:\.\d+){0,3})", tag)
    if match:
        track, version = match.groups()
        track_order = {"stable": 0, "testing": 1, "unstable": 2, None: 3}[track]
        numeric = tuple(-int(part) for part in version.split("."))
        return (1, track_order, numeric)
    return (2, 0, (tag,))


def target(data: Any, image: str, tag: str, *, helper_available: bool = True) -> dict[str, Any]:
    """Describe an exact image/tag choice against the booted identity."""
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        return _unavailable(C_('Unsupported {bazzite} image variant.', bazzite='Bazzite'))
    if not _valid_tag(tag):
        return _unavailable(C_('Unsupported {bazzite} image version.', bazzite='Bazzite'))
    snapshot = _snapshot(data)
    if isinstance(snapshot, dict):
        return snapshot
    _running, identity, current_image, current_tag = snapshot
    target_reference = f"ostree-image-signed:docker://{REGISTRY}/{image}:{tag}"
    same_deployment = image == current_image and tag == current_tag
    result = {"available": True, "reason": "", "token": _request_token(identity, image, tag),
              "identity": _request_token(identity, "identity", "identity"),
              "current_image": current_image, "current_tag": current_tag,
              "image": image, "tag": tag, "target_reference": target_reference,
              "desktop_environment_change": _desktop_environment(current_image) != _desktop_environment(image)}
    if same_deployment:
        result.update(available=False, reason=N_("This image and version are already running. Use Update to check for a newer version."))
    elif not helper_available:
        result.update(available=False, reason="The image-switch service is not installed.")
    return result


def verify_token(data: Any, token: str, image: str, tag: str,
                 *, helper_available: bool = True) -> dict[str, Any]:
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid image-switch confirmation token.")
    current = target(data, image, tag, helper_available=helper_available)
    if not current["available"] or current["token"] != token:
        raise RuntimeError(current["reason"] or "The running deployment changed after confirmation.")
    return current


def prepare_request(data: Any, image: str, tag: str,
                    *, helper_available: bool = True) -> dict[str, Any]:
    """Prepare the exact bounded systemd instance for a confirmed selection."""
    selected = target(data, image, tag, helper_available=helper_available)
    if not selected["available"]:
        raise RuntimeError(selected["reason"] or "The selected Bazzite image is unavailable.")
    instance = f"{selected['token']}-{image}-{tag}"
    unit = f"bazzite-portal-maintenance@image-switch-{instance}.service"
    if len(unit) > 255 or not INSTANCE_PATTERN.fullmatch(instance):
        raise ValueError("The selected image request cannot be represented safely.")
    return {"token": selected["token"], "image": image, "tag": tag,
            "instance": instance, "target_reference": selected["target_reference"]}


def _read_status(run_command: Callable[..., str]) -> dict[str, Any]:
    try:
        raw = run_command(["rpm-ostree", "status", "--json"], timeout=15)
        if not isinstance(raw, str) or len(raw.encode("utf-8", "replace")) > MAX_STATUS_BYTES:
            raise ValueError("oversized status")
        data = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError("RPM-OSTree returned malformed deployment status.") from error
    if not isinstance(data, dict):
        raise RuntimeError("RPM-OSTree returned malformed deployment status.")
    return data


def execute(token: str, image: str, tag: str,
            run_command: Callable[..., str] = cancel_pending._command,
            lock_factory: Callable[[], Any] = cancel_pending._pending_lock) -> str:
    """Revalidate a bounded choice and ask RPM-OSTree to stage exactly that target."""
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid image-switch confirmation token.")
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        raise ValueError("Unsupported Bazzite image variant.")
    if not _valid_tag(tag):
        raise ValueError("Unsupported Bazzite image version.")
    with lock_factory():
        state = _read_status(run_command)
        current = verify_token(state, token, image, tag)
        active = cancel_pending._active_operation(run_command)
        if active:
            raise RuntimeError(f"Cannot change images while {active} is running.")
        if tag not in _list_tags(image, run_command):
            raise RuntimeError("The selected Bazzite version is no longer available.")
        # Registry lookup may take its full timeout; refresh deployment
        # identity and transaction state immediately before the mutation.
        state = _read_status(run_command)
        current = verify_token(state, token, image, tag)
        active = cancel_pending._active_operation(run_command)
        if active:
            raise RuntimeError(f"Cannot change images while {active} is running.")
        # The target is assembled only from the bundled catalog and a
        # restricted version grammar. The full signed ref goes directly to RPM-OSTree.
        channel_switch._rebase(run_command, current["target_reference"])
        channel_switch.verify_switch_result(state, _read_status(run_command), current["target_reference"])
    return f"Bazzite is switching to {image}:{tag}. Restart to apply the staged deployment."


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if (len(argv) != 1 or len(argv[0]) > MAX_TAG_BYTES + 64 + 2 + 40
            or not INSTANCE_PATTERN.fullmatch(argv[0])):
        print("Usage: python3 -m bazzite_portal.maintenance.image_switch <token-image-tag>", file=sys.stderr)
        return 2
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("This helper must run as root.", file=sys.stderr)
        return 1
    match = INSTANCE_PATTERN.fullmatch(argv[0])
    assert match is not None
    token, image, tag = match.groups()
    if not _valid_tag(tag):
        print("Invalid image-switch version.", file=sys.stderr)
        return 2
    try:
        print(execute(token, image, tag))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error)[:1024], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
