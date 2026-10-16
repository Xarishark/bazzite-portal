"""Compact, read-only system identity for the System information page."""

import re

from bazzite_portal.maintenance.deployments import summarize_deployments
from bazzite_portal.core.i18n import C_, N_

MAX_PACKAGES = 512
MAX_PACKAGE_TEXT = 512
MAX_REFERENCE_LENGTH = 1024
_IMAGE_REFERENCE = re.compile(
    r"^(?:(?:ostree-(?:image-signed|unverified-image):docker://)|ostree-unverified-registry:)?ghcr\.io/ublue-os/"
    r"(?P<name>bazzite(?:-[a-z0-9][a-z0-9._-]*)?)"
    r"(?:(?P<digest>@sha256:[0-9a-fA-F]{64})|:(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?$"
)


def _unknown():
    unknown = N_("Unknown")
    return {"variant": unknown, "track": unknown, "packages": unknown}


def _reference_parts(value):
    if not isinstance(value, str) or not value or len(value) > MAX_REFERENCE_LENGTH:
        return None
    match = _IMAGE_REFERENCE.fullmatch(value)
    return match.groupdict() if match else None


def _variant(reference):
    parts = _reference_parts(reference)
    if not parts:
        return N_("Unknown")
    name = parts["name"]
    suffix = name[len("bazzite"):].strip("-")
    if not suffix:
        return "Bazzite"
    return "Bazzite " + " ".join(word.upper() if word == "nvidia" else word.title()
                                  for word in suffix.split("-"))


def _track(reference):
    parts = _reference_parts(reference)
    if not parts:
        return N_("Unknown")
    if parts["digest"]:
        return N_("Pinned Bazzite version")
    tag = parts["tag"]
    if tag == "stable":

        return N_("Stable")
    if tag == "testing":

        return N_("Testing")
    if tag and re.fullmatch(r"(?:testing-)?\d{2,4}\.\d{8}(?:\.\d+)?", tag):
        return C_("Pinned version · {tag}", tag=tag)
    return N_("Unknown")


def _applied_packages(raw_status, summary):
    # rpm-ostree's booted deployment `packages` field contains applied layer
    # identities. Requested packages are deliberately not used as a fallback.
    if not isinstance(raw_status, dict) or not isinstance(summary, dict) or not summary.get("known"):
        return N_("Unknown")
    if summary.get("engine") != "rpm-ostree":
        return N_("Unknown")
    deployments = raw_status.get("deployments")
    if not isinstance(deployments, list):
        return N_("Unknown")
    booted = [item for item in deployments if isinstance(item, dict) and item.get("booted") is True]
    if len(booted) != 1:
        return N_("Unknown")
    packages = booted[0].get("packages")
    if not isinstance(packages, list) or any(not isinstance(item, str) or not item.strip() for item in packages):
        return N_("Unknown")
    if len(packages) > MAX_PACKAGES:
        return C_("Unknown · exceeds display limit ({count} packages)", count=len(packages))
    if not packages:
        return N_("No packages layered")
    cleaned = []
    for package in packages:
        value = "".join(char for char in package if ord(char) >= 32 and ord(char) != 127).strip()
        if not value or len(value) > MAX_PACKAGE_TEXT:
            return N_("Unknown")
        cleaned.append(value)
    text = "\n".join(sorted(set(cleaned), key=str.casefold))
    return text if len(text.encode("utf-8")) <= 65536 else N_("Unknown · package list exceeds display limit")


def summarize_system_presentation(system_information, deployment_summary=None):
    """Return booted image variant, track and applied package-layer labels.

    ``system_information`` is the already-read result from system.py. Raw
    deployment JSON is used only for fields whose meaning depends on the
    booted deployment, and malformed or unsupported data stays Unknown.
    """
    result = _unknown()
    if not isinstance(system_information, dict):
        return result
    raw_status = system_information.get("deployment_status")
    if not isinstance(raw_status, dict):
        return result
    summary = deployment_summary or summarize_deployments(raw_status)
    if not isinstance(summary, dict) or not summary.get("known"):
        return result
    running = [entry for entry in summary.get("entries", [])
               if isinstance(entry, dict) and entry.get("role") == "Running now"]
    if len(running) != 1:
        return result
    reference = running[0].get("image")
    result["variant"] = _variant(reference)
    result["track"] = _track(reference)
    result["packages"] = _applied_packages(raw_status, summary)
    return result
