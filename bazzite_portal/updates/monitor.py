"""System-owned update jobs. Closing this client never stops their units."""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from bazzite_portal.updates.model import (
    SERVICES,
    UUPD_MODULES,
    METERED_SKIP_REASON,
    Snapshot,
    interpret,
    update_event,
    live_module_detail,
    journal_message,
    planned_modules,
    module_steps,
    pending_deployment,
    MAX_LOG_BYTES,
    MAX_LOG_LINES,
    MAX_MODULE_STEPS,
)
from bazzite_portal.updates import journal as update_journal
from bazzite_portal.maintenance import cancel_pending as cancel_pending_backend
from bazzite_portal.maintenance import rollback_cancel as rollback_cancel_backend
from bazzite_portal.maintenance import reset_layers as reset_layers_backend
from bazzite_portal.maintenance import cache_clear as cache_clear_backend
from bazzite_portal.maintenance import channel_switch as channel_switch_backend
from bazzite_portal.maintenance import image_switch as image_switch_backend
from bazzite_portal.maintenance import delete_deployments as delete_deployments_backend
from bazzite_portal.maintenance import logs as maintenance_logs
from bazzite_portal.maintenance import state as maintenance_state
from bazzite_portal.updates import trace as update_trace
from bazzite_portal.maintenance import service as maintenance_service
from bazzite_portal.maintenance import deployments as deployments_backend
from bazzite_portal.maintenance import runtime as maintenance_runtime
from bazzite_portal.core.i18n import C_, N_

UUPD_CONFIG_PATH = "/etc/uupd/config.json"
MAX_UUPD_CONFIG_BYTES = 64 * 1024
INVOCATION_ID = re.compile(r"^[0-9a-fA-F]{32}$")
HISTORY_REFRESH_SECONDS = 300
CACHE_TARGET_REFRESH_SECONDS = 60
_maintenance_records_lock = threading.Lock()
_maintenance_records = maintenance_state.load()
_maintenance_records_generation = max(
    (record.get("generation", 0) for record in _maintenance_records.values()), default=0)
_maintenance_live_requests = set()


_COMMAND_OUTPUT_TAIL_BYTES = 2 * 1024 * 1024


def _read_output_tail(stream, limit):
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    stream.seek(max(0, length - limit))
    return stream.read().decode("utf-8", errors="replace")


def command(argv, timeout=15):
    # Keep output on disk; stdout is structured data for many callers, so
    # unrelated loader and desktop warnings on stderr must not corrupt it.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        env = {**os.environ, "LC_ALL": "C"}
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                timeout=timeout, env=env, start_new_session=True)
        if result.returncode:
            # Bound the combined error text to the same tail size as before,
            # reserving space for both streams and preferring stderr first.
            stderr_text = _read_output_tail(stderr, _COMMAND_OUTPUT_TAIL_BYTES // 2).strip()
            stdout_text = _read_output_tail(stdout, _COMMAND_OUTPUT_TAIL_BYTES // 2).strip()
            text = "\n".join(part for part in (stderr_text, stdout_text) if part)
            raise RuntimeError(text or f"{argv[0]} exited with {result.returncode}")
        return _read_output_tail(stdout, _COMMAND_OUTPUT_TAIL_BYTES)


def service_state(unit):
    text = command(["systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,InvocationID,ExecMainStatus,Result,InactiveExitTimestampMonotonic,ActiveEnterTimestampMonotonic"])
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def configured_module_plan(path=UUPD_CONFIG_PATH):
    """Read UUPD configuration only; run on the monitor worker thread."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_UUPD_CONFIG_BYTES + 1)
    except FileNotFoundError:
        # UUPD documents these modules as enabled by default when its default
        # config file is absent. Runtime checks still decide what will run.
        return planned_modules()
    except OSError:
        return []
    if len(raw) > MAX_UUPD_CONFIG_BYTES:
        return []
    try:
        config_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return planned_modules(config_text)


def deployment_status_data():
    """Read one bounded rpm-ostree/bootc status document for management UI."""
    errors = []
    for argv in (["rpm-ostree", "status", "--json"], ["bootc", "status", "--json"]):
        try:
            raw = command(argv)
            if not isinstance(raw, str) or len(raw.encode("utf-8", errors="replace")) > channel_switch_backend.MAX_STATUS_BYTES:
                raise ValueError("Deployment status is oversized.")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Unexpected deployment status")
            if "deployments" not in data and "status" not in data:
                raise ValueError("Deployment status has an unknown format")
            return data, True, ""
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))
    return {}, False, "Could not inspect pending deployments: " + "; ".join(errors)


def deployment_state():
    data, known, error = deployment_status_data()
    if not known:
        return "", "", False, error
    deployment, label = pending_deployment(data)
    return deployment, label, True, ""


CANCEL_TEMPLATE = "bazzite-portal-maintenance@.service"
CANCEL_INSTANCE_PATTERN = re.compile(r"^bazzite-portal-maintenance@cancel-pending-([0-9a-f]{64})\.service$")
CANCEL_ACTIVE_STATES = ("active", "activating", "deactivating", "reloading")
CANCEL_INSTALLED_STATES = {"enabled", "disabled", "static", "indirect", "generated", "transient"}
MAX_CANCEL_INSTANCES = 32
LAYER_TEMPLATE = "bazzite-portal-maintenance@.service"
LAYER_INSTANCE_PATTERN = re.compile(r"^bazzite-portal-maintenance@reset-layers-([0-9a-f]{64})\.service$")
SELECTED_LAYER_TEMPLATE = "bazzite-portal-maintenance@.service"
SELECTED_LAYER_INSTANCE_PATTERN = re.compile(r"^bazzite-portal-maintenance@remove-selected-layers-([0-9a-f]{64}-[0-9a-f]{64})\.service$")
CACHE_TEMPLATE = "bazzite-portal-maintenance@.service"
CACHE_INSTANCE_PATTERN = re.compile(r"^bazzite-portal-maintenance@clear-download-cache-([0-9a-f]{64})\.service$")
CHANNEL_TEMPLATE = "bazzite-portal-maintenance@.service"
CHANNEL_INSTANCE_PATTERN = re.compile(r"^bazzite-portal-maintenance@channel-switch-([0-9a-f]{64})\.service$")
DELETE_TEMPLATE = "bazzite-portal-maintenance@.service"
DELETE_INSTANCE_PATTERN = re.compile(r"^bazzite-portal-maintenance@delete-deployment-([0-9a-f]{64})\.service$")
IMAGE_TEMPLATE = "bazzite-portal-maintenance@.service"
IMAGE_INSTANCE_PATTERN = re.compile(
    r"^bazzite-portal-maintenance@image-switch-(?:" + image_switch_backend.INSTANCE_PATTERN.pattern[1:-1] + r")\.service$"
)


def _operation_unit_matches(unit, *patterns):
    """Recognize running pre-upgrade units without offering legacy starts."""
    if not isinstance(unit, str):
        return False
    if any(pattern.fullmatch(unit) for pattern in patterns):
        return True
    for operation in ("cancel-pending", "reset-layers", "remove-selected-layers",
                      "clear-download-cache", "channel-switch", "delete-deployment", "image-switch"):
        prefix = f"bazzite-portal-{operation}@"
        if unit.startswith(prefix):
            translated = "bazzite-portal-maintenance@" + operation + "-" + unit[len(prefix):]
            return any(pattern.fullmatch(translated) for pattern in patterns)
    return False


def _legacy_unit_glob(pattern):
    for operation in ("channel-switch", "delete-deployment", "image-switch"):
        if operation in pattern.pattern:
            return f"bazzite-portal-{operation}@*.service"
    raise ValueError("Unknown maintenance operation pattern")


def _cancel_template_exists():
    try:
        text = command(["systemctl", "list-unit-files", "--no-pager", "--output=json", CANCEL_TEMPLATE])
    except RuntimeError as error:
        # systemctl exits 1 with an empty JSON list when this exact unit is absent.
        if str(error).strip() == "[]":
            return False
        raise
    records = json.loads(text)
    if not isinstance(records, list):
        raise ValueError("systemd returned malformed unit-file data")
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("unit_file") == CANCEL_TEMPLATE:
            return record.get("state") in CANCEL_INSTALLED_STATES
    return False


def _cancel_instances():
    text = command(["systemctl", "list-units", "--all", "--no-pager", "--output=json", "bazzite-portal-maintenance@cancel-pending-*.service", "bazzite-portal-cancel-pending@*.service"])
    records = json.loads(text)
    if not isinstance(records, list):
        raise ValueError("systemd returned malformed cancellation unit data")
    valid_units = []
    for record in records:
        if not isinstance(record, dict):
            continue
        unit = record.get("unit")
        if _operation_unit_matches(unit, CANCEL_INSTANCE_PATTERN):
            valid_units.append(unit)
    if len(valid_units) > MAX_CANCEL_INSTANCES:
        raise ValueError("Too many cancellation unit instances to inspect safely")
    states = []
    for unit in dict.fromkeys(valid_units):
        state = service_state(unit)
        if state.get("LoadState") == "not-found":
            continue
        if state.get("LoadState") != "loaded" or not state.get("ActiveState"):
            raise ValueError("Could not verify cancellation unit state for {unit}".format(unit=unit))
        state["unit"] = unit
        if not maintenance_service.valid_unit(unit) and state.get("ActiveState") not in CANCEL_ACTIVE_STATES:
            continue
        try:
            state["_timestamp"] = int(state.get("InactiveExitTimestampMonotonic", "0") or 0)
        except ValueError:
            state["_timestamp"] = 0
        states.append(state)
    return states


def _cancel_result_state(state):
    if state.get("ActiveState") == "failed" or state.get("Result") not in (None, "", "success"):
        return "failed"
    # Newly loaded instances also have Result=success and exit status zero.
    # An invocation must have actually run before those defaults mean success.
    if (state.get("Result") == "success" and state.get("ExecMainStatus") == "0"
            and INVOCATION_ID.fullmatch(state.get("InvocationID", ""))):
        return "success"
    if (state.get("ActiveState") == "inactive" and not state.get("InvocationID")
            and _unit_timestamp(state, "InactiveExitTimestampMonotonic") == 0):
        return "idle"
    return "unknown"


def cancel_pending_status():
    """Read helper availability and independent cancellation instance state."""
    try:
        template_available = _cancel_template_exists()
        instances = _cancel_instances()
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        return {"available": False, "unit": "", "state": "unknown", "error": str(error)[:1024]}

    active = [item for item in instances if item.get("ActiveState") in CANCEL_ACTIVE_STATES]
    if active:
        selected = max(active, key=lambda item: _unit_timestamp(item, "ActiveEnterTimestampMonotonic"))
        return {"available": template_available, "unit": selected["unit"], "state": "running", "invocation": selected.get("InvocationID", ""), "error": "" if template_available else "Cancellation service was removed while its instance is still running."}
    if instances:
        selected = max(instances, key=lambda item: item["_timestamp"])
        result_state = _cancel_result_state(selected)
        unit = selected["unit"]
    else:
        result_state, unit = "idle", ""
    if not template_available:
        return {"available": False, "unit": unit, "state": "unavailable", "error": "Pending-deployment cancellation service is not installed."}
    error = ""
    if result_state == "failed":
        error = selected.get("Result") or "Cancellation helper failed."
        if selected.get("ExecMainStatus") not in (None, "", "0"):
            error = f"{error} (exit {selected['ExecMainStatus']})"
    elif result_state == "unknown":
        error = "The latest cancellation result could not be verified."
    return {
        "available": template_available,
        "unit": unit,
        "state": result_state,
        "error": error[:1024],
        "invocation": selected.get("InvocationID", "") if instances else "",
    }


def layer_status():
    """Discover fixed layer-removal helpers and their most recent instance."""
    try:
        try:
            text = command(["systemctl", "list-unit-files", "--no-pager", "--output=json",
                            LAYER_TEMPLATE, SELECTED_LAYER_TEMPLATE])
        except RuntimeError as error:
            if str(error).strip() == "[]":
                text = "[]"
            else:
                raise
        records = json.loads(text)
        if not isinstance(records, list):
            raise ValueError("systemd returned malformed unit-file data")
        available_units = {row.get("unit_file") for row in records if isinstance(row, dict)
                           and row.get("state") in CANCEL_INSTALLED_STATES}
        available = LAYER_TEMPLATE in available_units
        selected_available = SELECTED_LAYER_TEMPLATE in available_units
        try:
            text = command(["systemctl", "list-units", "--all", "--no-pager", "--output=json", "bazzite-portal-*@*.service"])
        except RuntimeError as error:
            if str(error).strip() == "[]":
                text = "[]"
            else:
                raise
        rows = json.loads(text)
        if not isinstance(rows, list):
            raise ValueError("systemd returned malformed layer-reset unit data")
        units = list(dict.fromkeys(row.get("unit") for row in rows if isinstance(row, dict)
                                   and isinstance(row.get("unit"), str)
                                   and _operation_unit_matches(row["unit"], LAYER_INSTANCE_PATTERN,
                                                               SELECTED_LAYER_INSTANCE_PATTERN)))
        if len(units) > MAX_CANCEL_INSTANCES:
            raise ValueError("Too many layer-reset unit instances to inspect safely")
        states = []
        for unit in units:
            state = service_state(unit)
            if state.get("LoadState") == "not-found":
                continue
            if state.get("LoadState") != "loaded" or not state.get("ActiveState"):
                raise ValueError("Could not verify layer-reset state for {unit}".format(unit=unit))
            state["unit"] = unit
            if not maintenance_service.valid_unit(unit) and state.get("ActiveState") not in CANCEL_ACTIVE_STATES:
                continue
            state["_timestamp"] = _unit_timestamp(state, "InactiveExitTimestampMonotonic")
            states.append(state)
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        return {"available": False, "selected_available": False, "unit": "", "state": "unknown", "error": str(error)[:1024]}
    active = [item for item in states if item.get("ActiveState") in CANCEL_ACTIVE_STATES]
    selected = max(active or states, key=lambda item: _unit_timestamp(item, "ActiveEnterTimestampMonotonic") if item in active else item["_timestamp"], default=None)
    if selected and selected.get("ActiveState") in CANCEL_ACTIVE_STATES:
        return {"available": available, "selected_available": selected_available, "unit": selected["unit"], "state": "running", "invocation": selected.get("InvocationID", ""), "error": "" if available else "Package-layer reset service was removed while its instance is still running."}
    if not available:
        return {"available": False, "selected_available": selected_available, "unit": selected["unit"] if selected else "", "state": "unavailable", "error": "Package-layer reset service is not installed."}
    if not selected:
        return {"available": True, "selected_available": selected_available, "unit": "", "state": "idle", "error": ""}
    outcome = _cancel_result_state(selected)
    error = (selected.get("Result") or "Layer reset helper failed.") if outcome == "failed" else ("The latest layer reset result could not be verified." if outcome == "unknown" else "")
    if outcome == "failed" and selected.get("ExecMainStatus") not in (None, "", "0"):
        error += f" (exit {selected['ExecMainStatus']})"
    return {"available": True, "selected_available": selected_available, "unit": selected["unit"], "state": outcome, "error": error[:1024], "invocation": selected.get("InvocationID", "")}


def maintenance_output(status):
    if not status.get("unit"):
        return {"logs": [], "error": ""}
    return maintenance_logs.read(status["unit"], status.get("invocation", ""), command)


def rollback_status():
    """Read rollback unit state without starting or changing its transaction."""
    unit = "bazzite-portal-maintenance@rollback.service"
    try:
        state = service_state(unit)
        if state.get("ActiveState") not in CANCEL_ACTIVE_STATES:
            legacy = service_state("bazzite-portal-rollback.service")
            if legacy.get("LoadState") == "loaded" and legacy.get("ActiveState") in CANCEL_ACTIVE_STATES:
                return {"available": state.get("LoadState") == "loaded",
                        "unit": "bazzite-portal-rollback.service", "state": "running",
                        "invocation": legacy.get("InvocationID", ""), "error": ""}
        if state.get("LoadState") == "not-found":
            return {"available": False, "unit": unit, "invocation": "",
                    "state": "unavailable", "error": "Rollback service is not installed."}
        if state.get("LoadState") != "loaded" or not state.get("ActiveState"):
            raise RuntimeError("Could not verify rollback service state.")
        rows = json.loads(command(["systemctl", "list-units", "--all", "--no-pager", "--output=json",
                                   "bazzite-portal-maintenance@cancel-rollback-*.service"]))
        if not isinstance(rows, list):
            raise ValueError("systemd returned malformed rollback-cancellation instance data")
        units = list(dict.fromkeys(row.get("unit") for row in rows if isinstance(row, dict)
                                   and isinstance(row.get("unit"), str)
                                   and re.fullmatch(r"bazzite-portal-maintenance@cancel-rollback-[0-9a-f]{64}\.service",
                                                    row["unit"])))
        if len(units) > MAX_CANCEL_INSTANCES:
            raise ValueError("Too many rollback-cancellation instances to inspect safely")
        candidates = [(unit, state)]
        for candidate in units:
            candidate_state = service_state(candidate)
            if candidate_state.get("LoadState") == "not-found":
                continue
            if candidate_state.get("LoadState") != "loaded" or not candidate_state.get("ActiveState"):
                raise ValueError("Could not verify rollback-cancellation service state")
            candidates.append((candidate, candidate_state))
        active_candidates = [pair for pair in candidates if pair[1].get("ActiveState") in CANCEL_ACTIVE_STATES]
        unit, state = max(active_candidates or candidates,
                          key=lambda pair: _unit_timestamp(pair[1], "InactiveExitTimestampMonotonic"))
        active = state.get("ActiveState") in CANCEL_ACTIVE_STATES
        outcome = "running" if active else _cancel_result_state(state)
        error = ""
        if outcome == "failed":
            error = state.get("Result") or N_('Rollback failed')
            if state.get("ExecMainStatus") not in (None, "", "0"):
                error += f" (exit {state['ExecMainStatus']})"
        elif outcome == "unknown":
            error = "The latest rollback result could not be verified."
        return {"available": True, "unit": unit,
                "invocation": state.get("InvocationID", ""),
                "state": outcome, "error": error[:1024]}
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        return {"available": False, "unit": unit, "invocation": "",
                "state": "unknown", "error": str(error)[:1024]}


def _latest_maintenance_invocation(unit, started_at_us):
    """Read the newest validated invocation for this exact unit start."""
    if not maintenance_logs.read_only_valid_unit(unit):
        return ""
    try:
        seconds, micros = divmod(started_at_us, 1_000_000)
        output = command(["journalctl", "-b", "_SYSTEMD_UNIT=" + unit, "--since", f"@{seconds}.{micros:06d}",
                          "--no-pager", "--quiet", "-n", "128", "-o", "json"])
    except Exception:
        return ""
    latest = (0, "")
    for line in output.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("_SYSTEMD_UNIT") != unit:
            continue
        invocation = entry.get("_SYSTEMD_INVOCATION_ID", "")
        if not isinstance(invocation, str) or not INVOCATION_ID.fullmatch(invocation):
            continue
        try:
            timestamp = int(entry["__REALTIME_TIMESTAMP"])
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp < started_at_us:
            continue
        if timestamp >= latest[0]:
            latest = (timestamp, invocation.lower())
    return latest[1]


def _maintenance_discovery_predates_request(status, record):
    """Prove a retained inactive invocation is older than the latest request."""
    unit, invocation = status.get("unit", ""), status.get("invocation", "")
    requested_at = record.get("requested_at", 0)
    if (status.get("state") not in {"success", "failed", "unknown"}
            or not maintenance_logs.read_only_valid_unit(unit)
            or not isinstance(invocation, str) or not INVOCATION_ID.fullmatch(invocation)
            or type(requested_at) is not int or requested_at <= 0):
        return False
    try:
        output = command(["journalctl", "-b", "_SYSTEMD_UNIT=" + unit,
                          "_SYSTEMD_INVOCATION_ID=" + invocation.casefold(),
                          "--no-pager", "--quiet", "-n", "128", "-o", "json"])
        older = False
        for line in output.splitlines():
            try:
                entry = json.loads(line)
                if (not isinstance(entry, dict) or entry.get("_SYSTEMD_UNIT") != unit
                        or entry.get("_SYSTEMD_INVOCATION_ID", "").casefold() != invocation.casefold()):
                    continue
                timestamp = int(entry["__REALTIME_TIMESTAMP"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if 0 < timestamp < requested_at:
                older = True
                break
        if not older:
            return False
        current = service_state(unit)
        # Inventory and journal reads can race a reused unit. Never hide a live
        # operation or adopt an invocation different from the one just dated.
        return (current.get("LoadState") == "loaded"
                and current.get("ActiveState") in {"inactive", "failed"}
                and current.get("InvocationID", "").casefold() == invocation.casefold())
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return False


def _save_maintenance_records():
    try:
        maintenance_state.save(_maintenance_records)
    except (OSError, ValueError, TypeError):
        # Local history is best effort and must never block system operations.
        pass


def maintenance_viewed_results():
    """Recover exact read markers from this boot's durable request records."""
    with _maintenance_records_lock:
        return {("layer" if kind == "layers" else kind):
                (record["unit"], record.get("invocation", ""), record["generation"])
                for kind, record in _maintenance_records.items() if record.get("viewed") is True}


def mark_maintenance_viewed(kind, unit, invocation, generation):
    """Mark only the acknowledged request, never a newer concurrent result."""
    kind = "layers" if kind == "layer" else kind
    with _maintenance_records_lock:
        record = _maintenance_records.get(kind)
        if record is None or (record["unit"], record.get("invocation", ""),
                              record["generation"]) != (unit, invocation, generation):
            return
        record["viewed"] = True
        _save_maintenance_records()


def clear_maintenance_viewed():
    with _maintenance_records_lock:
        for record in _maintenance_records.values():
            record.pop("viewed", None)
        _save_maintenance_records()


def _maintenance_request(kind, unit, state="running", invocation="", error="", *, live=False):
    global _maintenance_records_generation
    with _maintenance_records_lock:
        previous = _maintenance_records.get(kind)
        if state == "running" or previous is None:
            _maintenance_records_generation += 1
            generation = _maintenance_records_generation
        else:
            # A request generation identifies one invocation of the API,
            # stable across running, completion, and recovery snapshots.
            generation = previous.get("generation", _maintenance_records_generation)
        record = {
            "unit": unit, "invocation": invocation, "state": state,
            "error": error[:1024], "updated": time.monotonic(),
            "generation": generation,
            "requested_at": (time.time_ns() // 1_000 if state == "running"
                              else (previous or {}).get("requested_at", 0)),
        }
        if (previous and previous.get("viewed") is True and state != "running"
                and (previous["unit"], previous.get("invocation", ""), previous["generation"])
                == (unit, invocation, generation)):
            record["viewed"] = True
        _maintenance_records[kind] = record
        if live:
            _maintenance_live_requests.add((kind, generation))
        _save_maintenance_records()
        return dict(record)


def _maintenance_request_is_live(kind, record):
    with _maintenance_records_lock:
        return (kind, record.get("generation")) in _maintenance_live_requests


def _finish_live_maintenance_request(kind, generation):
    with _maintenance_records_lock:
        _maintenance_live_requests.discard((kind, generation))


def _maintenance_request_status(kind):
    with _maintenance_records_lock:
        record = _maintenance_records.get(kind)
        return dict(record) if record else None


def _is_proven_prestart_rejection(error, unit):
    """Return true only when evidence proves authorization stopped this unit before start."""
    accepted_reasons = {
        "Access denied",
        "Interactive authentication required",
        "Failed to authenticate",
    }
    prefix = f"Failed to start {unit}: "
    return any(
        line.strip().startswith(prefix) and line.strip()[len(prefix):] in accepted_reasons
        for line in str(error).splitlines()
    )


def _start_maintenance_unit(kind, unit):
    """Wait for one helper and retain its exact unit result across systemd GC."""
    started_at_us = time.time_ns() // 1_000
    record = _maintenance_request(kind, unit, live=True)
    try:
        command(["systemctl", "start", "--wait", unit], timeout=None)
    except Exception as error:
        invocation = _latest_maintenance_invocation(unit, started_at_us)
        state = "failed" if invocation or _is_proven_prestart_rejection(error, unit) else "unknown"
        _maintenance_request(kind, unit, state, invocation, str(error))
        raise
    else:
        invocation = _latest_maintenance_invocation(unit, started_at_us)
        # A zero exit from start --wait is completion evidence; the journal query
        # supplies the log identity. Without that identity leave the outcome
        # unknown so the UI cannot display logs from an earlier invocation.
        _maintenance_request(kind, unit, "success" if invocation else "unknown", invocation)
    finally:
        _finish_live_maintenance_request(kind, record["generation"])


def cache_status(target=None):
    """Read cache-helper availability and its latest fixed-unit invocation."""
    try:
        try:
            text = command(["systemctl", "list-unit-files", "--no-pager", "--output=json", CACHE_TEMPLATE])
        except RuntimeError as error:
            if str(error).strip() == "[]":
                text = "[]"
            else:
                raise
        records = json.loads(text)
        if not isinstance(records, list):
            raise ValueError("systemd returned malformed cache-helper unit data")
        installed = any(isinstance(row, dict) and row.get("unit_file") == CACHE_TEMPLATE
                        and row.get("state") in CANCEL_INSTALLED_STATES for row in records)
        try:
            text = command(["systemctl", "list-units", "--all", "--no-pager", "--output=json",
                            "bazzite-portal-maintenance@clear-download-cache-*.service", "bazzite-portal-clear-download-cache@*.service"])
        except RuntimeError as error:
            if str(error).strip() == "[]":
                text = "[]"
            else:
                raise
        rows = json.loads(text)
        if not isinstance(rows, list):
            raise ValueError("systemd returned malformed cache-helper instance data")
        units = list(dict.fromkeys(row.get("unit") for row in rows if isinstance(row, dict)
                                   and isinstance(row.get("unit"), str)
                                   and _operation_unit_matches(row["unit"], CACHE_INSTANCE_PATTERN)))
        if len(units) > MAX_CANCEL_INSTANCES:
            raise ValueError("Too many cache-helper instances to inspect safely")
        states = []
        for unit in units:
            state = service_state(unit)
            if state.get("LoadState") == "not-found":
                continue
            if state.get("LoadState") != "loaded" or not state.get("ActiveState"):
                raise ValueError("Could not verify cache-helper state for {unit}".format(unit=unit))
            state["unit"] = unit
            if not maintenance_service.valid_unit(unit) and state.get("ActiveState") not in CANCEL_ACTIVE_STATES:
                continue
            state["_timestamp"] = _unit_timestamp(state, "InactiveExitTimestampMonotonic")
            states.append(state)
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        return {"available": False, "unit": "", "state": "unknown", "error": str(error)[:1024]}
    active = [item for item in states if item.get("ActiveState") in CANCEL_ACTIVE_STATES]
    selected = max(active or states, key=lambda item: _unit_timestamp(item, "ActiveEnterTimestampMonotonic")
                   if item in active else item["_timestamp"], default=None)
    if selected and selected.get("ActiveState") in CANCEL_ACTIVE_STATES:
        return {"available": installed, "unit": selected["unit"], "state": "running",
                "invocation": selected.get("InvocationID", ""),
                "error": "" if installed else "Download-cache service was removed while its instance is still running."}
    if not installed:
        return {"available": False, "unit": selected["unit"] if selected else "", "state": "unavailable",
                "error": "Download-cache cleanup service is not installed."}
    if not selected:
        return {"available": True, "unit": "", "state": "idle", "error": ""}
    outcome = _cancel_result_state(selected)
    error = (selected.get("Result") or "Download-cache helper failed.") if outcome == "failed" else (
        "The latest cache cleanup result could not be verified." if outcome == "unknown" else "")
    if outcome == "failed" and selected.get("ExecMainStatus") not in (None, "", "0"):
        error += f" (exit {selected['ExecMainStatus']})"
    return {"available": True, "unit": selected["unit"], "state": outcome,
            "error": error[:1024], "invocation": selected.get("InvocationID", "")}


def download_cache_target(deployment_status=None):
    """Read a bounded deployment/ref identity for a confirmation prompt."""
    try:
        data = deployment_status
        if data is None:
            data = json.loads(command(["rpm-ostree", "status", "--json"]))
        if not isinstance(data, dict):
            raise ValueError("RPM-OSTree returned malformed deployment status.")
        refs = cache_clear_backend._read_refs(data, command)
        return cache_clear_backend.target(data, refs)
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        return {"available": False, "reason": str(error)[:1024], "token": "", "identity": "", "refs": []}


def _fixed_deployment_helper_status(template, pattern):
    try:
        try:
            files_text = command(["systemctl", "list-unit-files", "--no-pager", "--output=json", template])
        except RuntimeError as error:
            if str(error).strip() == "[]":
                files_text = "[]"
            else:
                raise
        files = json.loads(files_text)
        if not isinstance(files, list):
            raise ValueError("systemd returned malformed deployment-helper unit data")
        installed = any(isinstance(row, dict) and row.get("unit_file") == template
                        and row.get("state") in CANCEL_INSTALLED_STATES for row in files)
        try:
            units_text = command(["systemctl", "list-units", "--all", "--no-pager", "--output=json",
                                  template.replace("@.service", "@*.service"), _legacy_unit_glob(pattern)])
        except RuntimeError as error:
            if str(error).strip() == "[]":
                units_text = "[]"
            else:
                raise
        rows = json.loads(units_text)
        if not isinstance(rows, list):
            raise ValueError("systemd returned malformed deployment-helper instance data")
        units = list(dict.fromkeys(row.get("unit") for row in rows if isinstance(row, dict)
                                   and isinstance(row.get("unit"), str)
                                   and _operation_unit_matches(row["unit"], pattern)))
        if len(units) > MAX_CANCEL_INSTANCES:
            raise ValueError("Too many deployment-helper instances to inspect safely")
        states = []
        for unit in units:
            state = service_state(unit)
            if state.get("LoadState") == "not-found":
                continue
            if state.get("LoadState") != "loaded" or not state.get("ActiveState"):
                raise ValueError("Could not verify deployment-helper service state")
            state["unit"] = unit
            if not maintenance_service.valid_unit(unit) and state.get("ActiveState") not in CANCEL_ACTIVE_STATES:
                continue
            state["_timestamp"] = _unit_timestamp(state, "InactiveExitTimestampMonotonic")
            states.append(state)
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        return {"available": False, "state": "unknown", "unit": "", "invocation": "",
                "error": str(error)[:1024]}
    active = [item for item in states if item.get("ActiveState") in CANCEL_ACTIVE_STATES]
    selected = max(active or states, key=lambda item: _unit_timestamp(item, "ActiveEnterTimestampMonotonic")
                   if item in active else item["_timestamp"], default=None)
    if selected and selected.get("ActiveState") in CANCEL_ACTIVE_STATES:
        state = "running"
    elif not selected:
        state = "idle"
    else:
        state = _cancel_result_state(selected)
    outcome_error = ""
    if state == "failed":
        outcome_error = selected.get("Result") or N_("Deployment operation failed.")
        if selected.get("ExecMainStatus") not in (None, "", "0"):
            outcome_error += f" (exit {selected['ExecMainStatus']})"
    elif state == "unknown":
        outcome_error = "The latest deployment-helper result could not be verified."
    return {"available": installed, "state": state,
            "unit": selected["unit"] if selected else "",
            "invocation": selected.get("InvocationID", "") if selected else "",
            "error": outcome_error[:1024] if installed else "Deployment management service is not installed."}


def _channel_switch_status():
    return _fixed_deployment_helper_status(CHANNEL_TEMPLATE, CHANNEL_INSTANCE_PATTERN)


def image_switch_status(deployment_status=None):
    """Read image-switch service state and, when supplied, current image identity."""
    helper = _fixed_deployment_helper_status(IMAGE_TEMPLATE, IMAGE_INSTANCE_PATTERN)
    request = _maintenance_request_status("image")
    if request and IMAGE_INSTANCE_PATTERN.fullmatch(request.get("unit", "")):
        if helper.get("state") != "running":
            helper.update({key: request.get(key, "") for key in ("state", "unit", "invocation", "error")})
        helper["_request_generation"] = request.get("generation")
    if deployment_status is not None:
        parsed = image_switch_backend._snapshot(deployment_status)
        if isinstance(parsed, dict):
            helper["deployment_error"] = parsed.get("reason", "")
        else:
            _running, _identity, current_image, current_tag = parsed
            helper["current_image"] = current_image
            helper["current_tag"] = current_tag
    return helper


def _image_switch_service_is_running():
    status = _fixed_deployment_helper_status(IMAGE_TEMPLATE, IMAGE_INSTANCE_PATTERN)
    request = _maintenance_request_status("image")
    if request and IMAGE_INSTANCE_PATTERN.fullmatch(request.get("unit", "")):
        if request.get("state") in {"running", "unknown"}:
            status.update(state=request["state"], error=request.get("error", ""))
    if status.get("state") == "unknown":
        raise RuntimeError(status.get("error") or "Could not verify image-switch state.")
    return status.get("state") == "running"


def delete_deployment_status(deployment_status=None):
    helper = _fixed_deployment_helper_status(DELETE_TEMPLATE, DELETE_INSTANCE_PATTERN)
    try:
        if deployment_status is None:
            raw = command(["rpm-ostree", "status", "--json"])
            if len(raw.encode("utf-8")) > channel_switch_backend.MAX_STATUS_BYTES:
                raise ValueError("Deployment status is oversized.")
            deployment_status = json.loads(raw)
        result = delete_deployments_backend.targets(deployment_status)
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
        result = {"available": False, "reason": str(error)[:1024], "deployments": []}
    if not helper["available"]:
        result.update(available=False, reason="Deployment deletion service is not installed.")
    result.update({key: value for key, value in helper.items() if key != "available"})
    request = _maintenance_request_status("delete")
    if request and helper.get("state") != "running" and DELETE_INSTANCE_PATTERN.fullmatch(request.get("unit", "")):
        result.update({key: request.get(key, "") for key in ("state", "unit", "invocation", "error")})
    return result


def _delete_service_is_running():
    status = _fixed_deployment_helper_status(DELETE_TEMPLATE, DELETE_INSTANCE_PATTERN)
    request = _maintenance_request_status("delete")
    if request and request.get("state") in {"running", "unknown"}:
        status.update(state=request["state"], error=request.get("error", ""))
    if status.get("state") == "unknown":
        raise RuntimeError(status.get("error") or "Could not verify deployment deletion state.")
    return status.get("state") == "running"


def channel_status(deployment_status=None):
    """Read the booted channel target and fixed helper state on a worker."""
    helper = _channel_switch_status()
    if deployment_status is None:
        try:
            raw = command(["rpm-ostree", "status", "--json"])
            if not isinstance(raw, str) or len(raw.encode("utf-8", errors="replace")) > channel_switch_backend.MAX_STATUS_BYTES:
                raise ValueError("RPM-OSTree deployment status is oversized.")
            deployment_status = json.loads(raw)
        except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            result = channel_switch_backend.target(None)
            result["reason"] = str(error)[:1024]
        else:
            result = channel_switch_backend.target(deployment_status, helper_available=helper["available"])
    else:
        result = channel_switch_backend.target(deployment_status, helper_available=helper["available"])
    result.update({key: value for key, value in helper.items() if key != "available"})
    result["available"] = bool(result.get("available") and helper["available"]
                               and helper.get("state") != "unknown")
    request = _maintenance_request_status("channel")
    request_match = CHANNEL_INSTANCE_PATTERN.fullmatch(request.get("unit", "")) if request else None
    if (request and request_match and request_match.group(1) == result.get("token")
            and (not result.get("unit") or result.get("unit") == request["unit"])):
        if result.get("state") != "running" and request.get("state") != "running":
            result.update({key: request.get(key, "") for key in ("state", "unit", "invocation", "error")})
        elif request.get("state") == "running":
            result.update({"state": "running", "unit": request["unit"], "invocation": ""})
        result["_request_generation"] = request.get("generation")
    if result.get("state") == "unknown":
        result["available"] = False
    if helper.get("state") != "running":
        unit_match = CHANNEL_INSTANCE_PATTERN.fullmatch(result.get("unit", ""))
        if (result.get("state") in ("success", "failed")
                and (not result.get("token") or not unit_match or unit_match.group(1) != result["token"])):
            result["state"] = "idle"
            result["error"] = ""
    return result


def _channel_service_is_running():
    status = _channel_switch_status()
    if status["state"] == "unknown":
        raise RuntimeError(status["error"] or "Could not verify channel-switch state.")
    if status["state"] == "running":
        return True
    request = _maintenance_request_status("channel")
    if request:
        if request.get("state") == "unknown":
            raise RuntimeError(request.get("error") or "The previous channel-switch result could not be verified.")
        if request.get("state") == "running":
            return True
    if _delete_service_is_running():
        raise RuntimeError(N_("Saved-deployment deletion is running. Wait for it to finish."))
    if _image_switch_service_is_running():
        raise RuntimeError(C_('{bazzite} is changing images. Wait for it to finish.', bazzite='Bazzite'))
    return False


def _unit_timestamp(state, property_name):
    try:
        return int(state.get(property_name, "0") or 0)
    except (TypeError, ValueError):
        return 0


def _managed_operation_active():
    for unit in SERVICES:
        state = service_state(unit)
        load = state.get("LoadState")
        if load == "not-found":
            continue
        active = state.get("ActiveState")
        if load != "loaded" or not active:
            raise RuntimeError("Could not verify system operation state for {unit}.".format(unit=unit))
        if active in CANCEL_ACTIVE_STATES:
            return unit
    return ""


def latest_journal_invocation(loaded_units):
    """Find the newest bounded managed invocation from journal metadata."""
    latest = None
    errors = []
    read_succeeded = False
    for unit in SERVICES:
        if unit not in loaded_units:
            continue
        try:
            text = command(["journalctl", "-b", "-u", unit, "--no-pager", "--quiet", "-n", "256", "-o", "json"])
            read_succeeded = True
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))
            continue
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("_SYSTEMD_UNIT") != unit:
                continue
            run = entry.get("_SYSTEMD_INVOCATION_ID", "")
            if not isinstance(run, str) or not INVOCATION_ID.fullmatch(run):
                continue
            try:
                timestamp = int(entry.get("__REALTIME_TIMESTAMP", "0"))
            except (TypeError, ValueError):
                continue
            candidate = (timestamp, unit, run.lower())
            if latest is None or candidate[0] > latest[0]:
                latest = candidate
    if latest:
        return latest[1], latest[2], ""
    if read_succeeded or not loaded_units:
        return "", "", ""
    return "", "", "Could not inspect the UUPD journal. " + "; ".join(errors)


def _maintenance_job_is_queued(unit):
    """Inspect the typed systemd job table, never human-formatted output."""
    text = command([
        "busctl", "--system", "--json=short", "call", "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "ListJobs",
    ])
    if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
        raise ValueError("Could not verify systemd manager jobs.")
    result = json.loads(text)
    if (not isinstance(result, dict) or result.get("type") != "a(usssoo)"
            or not isinstance(result.get("data"), list) or len(result["data"]) != 1
            or not isinstance(result["data"][0], list)):
        raise ValueError("Could not verify systemd manager jobs.")
    jobs = result["data"][0]
    for row in jobs:
        if (not isinstance(row, list) or len(row) != 6
                or type(row[0]) is not int or not 0 < row[0] <= 0xffffffff
                or any(not isinstance(value, str) or not value for value in row[1:])
                or any(not re.fullmatch(r"/(?:[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)?", value)
                       for value in row[4:])):
            raise ValueError("Could not verify systemd manager jobs.")
    return any(row[1] == unit for row in jobs)


class Monitor:
    def __init__(self, changed, scheduler):
        self.changed, self.scheduler = changed, scheduler
        self.snapshot = Snapshot()
        self._lock = threading.RLock()
        self._checking = False
        self._recheck = False
        self._closed = False
        self._journal = None
        self._journal_run = ""
        self._journal_generation = 0
        self._journal_unit = ""
        self._journal_historical = False
        self._history_checked_at = 0.0
        self._journal_failed = ""
        self._finish_run = ""
        self._final_module_evidence = None
        self._journal_finished = set()
        self._lines = deque()
        self._line_bytes = 0
        self._failures = []
        self._stage = ""
        self._modules = {}
        self._active_module = ""
        self._progress_percent = None
        self._uupd_terminal = None
        self._unmapped_module_failure = False
        self._journal_publish_pending = None
        self._cache_target_checked_at = 0.0
        self._cache_target = {"available": False, "reason": "", "token": "", "identity": "", "refs": []}
        # systemd may briefly omit InvocationID while an instance is still
        # active. Keep the last validated identity per helper kind so a
        # transient property gap cannot retarget an open log view.
        self._maintenance_identity = {}
        self._maintenance_identity_generation = {}
        self._management_revision = 0

    def _stable_maintenance_status(self, kind, status):
        """Preserve a helper's last known unit/invocation across status gaps."""
        previous = self._maintenance_identity.get(kind, ("", ""))
        request_generation = status.pop("_request_generation", None)
        if (request_generation is not None
                and self._maintenance_identity_generation.get(kind) != request_generation):
            previous = ("", "")
            self._maintenance_identity_generation[kind] = request_generation
        unit = status.get("unit", "")
        invocation = status.get("invocation", "")
        running = status.get("state") == "running"
        if unit and status.get("state") != "unavailable":
            if unit != previous[0]:
                previous = (unit, "")
            if INVOCATION_ID.fullmatch(invocation):
                previous = (unit, invocation)
            elif unit == previous[0]:
                invocation = previous[1]
            self._maintenance_identity[kind] = previous
        elif status.get("state") in ("unknown", "unavailable") and previous[0]:
            # State discovery can race systemd's unit GC. Preserve only the
            # identity; callers still see the honest unknown/unavailable state.
            unit, invocation = previous
        elif not running and not unit:
            self._maintenance_identity.pop(kind, None)
        status = dict(status)
        status["unit"] = unit
        status["invocation"] = invocation
        if request_generation is not None:
            status["_request_generation"] = request_generation
        return status

    def _maintenance_status_with_request(self, kind, status):
        """Use request-worker evidence when systemd has already GC'd its unit."""
        record = _maintenance_request_status(kind)
        if not record:
            return status
        unit = status.get("unit", "")
        if (kind == "rollback" and status.get("available") is True
                and unit == "bazzite-portal-maintenance@rollback.service"
                and status.get("state") == "idle" and not status.get("invocation")
                and re.fullmatch(r"bazzite-portal-maintenance@cancel-rollback-[0-9a-f]{64}\.service",
                                 record["unit"])):
            # The unused fixed unit is an availability placeholder, not a new
            # operation. Preserve the cancellation's exact result after its
            # dynamic instance is collected, including terminal worker records.
            status = {**status, "unit": ""}
            unit = ""
        if (unit and unit != record["unit"] and status.get("available") is True
                and _maintenance_discovery_predates_request(status, record)):
            # systemd retains failed instances longer than successful ones. An
            # old failure must not replace a newer request whose unit was GC'd.
            # Refresh the record after the blocking reads so completion or a
            # still newer request cannot be replaced with its earlier state.
            latest_record = _maintenance_request_status(kind)
            if (latest_record is None
                    or latest_record.get("requested_at", 0) < record.get("requested_at", 0)):
                return status
            record = latest_record
            # Continue normal reconciliation with only stale discovery removed.
            status = {**status, "unit": "", "invocation": "", "state": "idle", "error": ""}
            unit = ""
        if (record.get("state") == "running" and status.get("available") is True
                and status.get("state") == "idle"
                and (not unit or (kind == "rollback" and unit == "bazzite-portal-maintenance@rollback.service"
                                  and (record["unit"] == unit or re.fullmatch(
                                      r"bazzite-portal-maintenance@cancel-rollback-[0-9a-f]{64}\.service",
                                      record["unit"])) and not status.get("invocation")))):
            # Fixed rollback units remain discoverable after their invocation
            # has been collected, unlike the dynamic helper instances.
            # The request thread can be waiting for PolicyKit while systemd has
            # not yet created the unit or queued a job. That is not evidence of
            # unit GC until the in-process start call has returned.
            if _maintenance_request_is_live(kind, record):
                return self._maintenance_request_status_snapshot(status, record)
            # The agent may restart after systemd has garbage-collected a
            # completed service. Check the manager job table before resolving
            # the stale running record; if a start is still queued, keep it running.
            try:
                if _maintenance_job_is_queued(record["unit"]):
                    return self._maintenance_request_status_snapshot(status, record)
            except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                return self._maintenance_request_status_snapshot(status, record)

            # Journald can prove which invocation belongs to the durable request,
            # but unit GC means its final systemd result is no longer available.
            # Preserve that invocation for exact logs and report unknown rather
            # than leaving a false running spinner or guessing success/failure.
            invocation = _latest_maintenance_invocation(record["unit"], record.get("requested_at", 0))
            detail = "The operation outcome could not be verified after systemd collected the service."
            _maintenance_request(kind, record["unit"], "unknown", invocation, detail)
            settled = _maintenance_request_status(kind)
            if settled is None:
                return status
            merged = self._maintenance_request_status_snapshot(status, settled)
            merged["_execution_idle"] = True
            return merged
        if (record.get("state") == "unknown" and status.get("available") is True
                and record.get("error", "").startswith(
                    "The operation outcome could not be verified after systemd collected the service.")
                and status.get("state") == "idle"
                and (not unit or (kind == "rollback" and unit == "bazzite-portal-maintenance@rollback.service"
                                  and (record["unit"] == unit or re.fullmatch(
                                      r"bazzite-portal-maintenance@cancel-rollback-[0-9a-f]{64}\.service",
                                      record["unit"])) and not status.get("invocation")))):
            merged = self._maintenance_request_status_snapshot(status, record)
            # Unknown completion is historical, not proof of a live operation.
            # Recheck execution on every refresh before unblocking other actions.
            try:
                merged["_execution_idle"] = (not _maintenance_request_is_live(kind, record)
                                             and not _maintenance_job_is_queued(record["unit"]))
            except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                pass
            return merged
        if (record.get("state") == "unknown" and status.get("available") is True
            and not record.get("error", "").startswith(
                "The operation outcome could not be verified after systemd collected the service.")
            and status.get("state") == "idle" and not unit):
            # A timed-out systemctl client leaves execution uncertain. Once a
            # fresh unit inventory says no helper instance exists, also check
            # the manager's current jobs before allowing a retry. This only
            # reports that no operation is running; it makes no claim about
            # whether the previous operation completed.
            try:
                if _maintenance_job_is_queued(record["unit"]):
                    return self._maintenance_request_status_snapshot(status, record)
            except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                return self._maintenance_request_status_snapshot(status, record)

            latest = _maintenance_request_status(kind)
            if latest is None:
                return status
            if latest.get("generation") != record.get("generation"):
                return self._maintenance_request_status_snapshot(status, latest)
            detail = N_("Previous request could not be confirmed. No operation is currently running. Try again.")
            previous_error = record.get("error", "")
            if previous_error:
                suffix = " Previous request error: "
                detail += suffix + previous_error[:max(0, 1024 - len(detail) - len(suffix))]
            return {
                **status,
                "unit": record["unit"],
                "invocation": record.get("invocation", ""),
                "state": "failed",
                "error": detail,
                "_request_generation": record["generation"],
            }
        if unit:
            # Current systemd discovery wins over an older worker record. If
            # it is the same instance, retain the worker's exact log identity.
            if unit == record["unit"]:
                merged = dict(status)
                merged["_request_generation"] = record["generation"]
                if record["state"] == "running":
                    status_invocation = status.get("invocation", "")
                    requested_at = record.get("requested_at", 0)
                    if (status.get("state") in ("success", "failed", "unknown")
                            and INVOCATION_ID.fullmatch(status_invocation)
                            and requested_at
                            and _latest_maintenance_invocation(unit, requested_at)
                            == status_invocation.casefold()):
                        # After an agent restart, the exact systemd invocation
                        # can finish before the request worker returns. Adopt
                        # only a journal invocation proven newer than this
                        # durable request timestamp.
                        _maintenance_request(kind, unit, status["state"], status_invocation,
                                             status.get("error", ""))
                        merged.update(state=status["state"], invocation=status_invocation,
                                      error=status.get("error", ""))
                        return merged
                    previous_invocation = self._maintenance_identity.get(kind, ("", ""))[1]
                    if status.get("state") != "running":
                        merged["state"] = "running"
                    if (status.get("state") != "running"
                            or not INVOCATION_ID.fullmatch(merged.get("invocation", ""))
                            or merged.get("invocation", "").casefold() == previous_invocation.casefold()):
                        merged["invocation"] = ""
                elif status.get("state") != "running":
                    # The worker's --wait result and post-start journal scan
                    # identify this invocation more precisely than retained
                    # properties from a reused systemd instance.
                    merged["state"] = record["state"]
                    merged["invocation"] = record.get("invocation", "")
                    merged["error"] = record.get("error", "")
                return merged
            return status
        merged = dict(status)
        merged.update({key: record[key] for key in ("unit", "invocation", "state", "error")})
        merged["_request_generation"] = record["generation"]
        return merged

    @staticmethod
    def _maintenance_request_status_snapshot(status, record):
        """Preserve a request record while a reconciliation check is inconclusive."""
        merged = dict(status)
        merged.update({key: record[key] for key in ("unit", "invocation", "state", "error")})
        merged["_request_generation"] = record["generation"]
        return merged

    def _cache_target_needs_refresh(self, deployment, cancel, layers, cache, now=None,
                                    management_input=None, maintenance_statuses=None):
        """Refresh cached confirmation identity on state changes or idle expiry."""
        now = time.monotonic() if now is None else now
        maintenance_statuses = maintenance_statuses or {}
        with self._lock:
            previous = self.snapshot
            identity_changed = bool(previous.deployment or deployment) and previous.deployment != deployment
            current_identity = cache_clear_backend._deployment_identity(management_input)
            previous_identity = cache_clear_backend._deployment_identity(
                previous.management.get("deployment_status") if isinstance(previous.management, dict) else None)
            deployment_set_changed = current_identity != previous_identity
            terminal_state_changed = any(
                getattr(previous, field) != current and current != "running"
                for field, current in (("cancel_state", cancel["state"]),
                                       ("layer_state", layers["state"]),
                                       ("cache_state", cache["state"]))
            )
            terminal_state_changed = terminal_state_changed or any(
                getattr(previous, f"{name}_state") != status.get("state")
                and status.get("state") != "running"
                for name, status in maintenance_statuses.items()
            )
            idle_expired = now - self._cache_target_checked_at >= CACHE_TARGET_REFRESH_SECONDS
        return bool(cache["available"] and cache["state"] != "running"
                    and (identity_changed or deployment_set_changed or terminal_state_changed or idle_expired))

    def refresh(self):
        with self._lock:
            if self._closed:
                return
            if self._checking:
                self._recheck = True
                return
            self._checking = True
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self):
        try:
            discovery_error = ""
            states = [(unit, service_state(unit)) for unit in SERVICES]
            loaded = [(u, s) for u, s in states if s.get("LoadState") == "loaded"]
            plan = configured_module_plan() if any(u.startswith("uupd") for u, _ in loaded) else []
            active = [(u, s) for u, s in loaded if s.get("ActiveState") in ("active", "activating", "deactivating", "reloading")]
            candidates = active or [(u, s) for u, s in loaded if s.get("InvocationID")]
            selected = max(candidates, key=lambda pair: int(pair[1].get("InactiveExitTimestampMonotonic", "0") or "0")) if candidates else ("", {})
            if not active and self._journal_historical and self._journal_run:
                # A retained systemd InvocationID may belong to an older unit
                # run. Keep the journal-selected newer invocation until the
                # next bounded journal reconciliation confirms another one.
                selected = (self._journal_unit, {})
                candidates = []
            if not candidates:
                with self._lock:
                    remembered_run, remembered_unit = self._journal_run, self._journal_unit
                if remembered_run:
                    selected = (remembered_unit, {})
                    if not self._journal_historical:
                        # The remembered live invocation lost its systemd ID.
                        # Recheck history now in case a newer run completed while
                        # this monitor was inactive.
                        self._history_checked_at = 0.0
                    self._journal_historical = True
            # A completed systemd unit can retain an older InvocationID after
            # a newer manual update has run and been garbage-collected. Reconcile
            # journal metadata even when systemd still offers a candidate ID.
            # Active jobs reset the timer so their final invocation is checked
            # as soon as the unit stops.
            now = time.monotonic()
            if active:
                self._history_checked_at = 0.0
            elif now - self._history_checked_at >= HISTORY_REFRESH_SECONDS:
                self._history_checked_at = now
                history_unit, history_run, history_error = latest_journal_invocation({u for u, _ in loaded})
                selected_run = self._journal_run if self._journal_historical else (
                    selected[1].get("InvocationID", "") or self._journal_run)
                if history_run and history_run != selected_run:
                    self._load_historical_journal(history_unit, history_run)
                    selected = (history_unit, {})
                    candidates = []
                elif history_error:
                    discovery_error = history_error
            unit, state = selected
            run = state.get("InvocationID", "") or (self._journal_run if not candidates else "")
            running = bool(active)
            if run and run != self._journal_run:
                self._start_journal(run)
                self._journal_unit = unit
                self._journal_historical = False
            if run and running:
                # UUPD detection/progress must not wait behind deployment,
                # maintenance eligibility and per-operation journal queries.
                self._publish_running_service(unit, run, plan)
            if run and not running and run not in self._journal_finished and self._finish_run != run:
                self._finish_run = run
                threading.Thread(target=self._finish_journal, args=(run,), daemon=True).start()
            deployment_data, status_available, status_error = deployment_status_data()
            deployment, label = pending_deployment(deployment_data) if status_available else ("", "")
            error = status_error if not status_available else ""
            cancel = cancel_pending_status()
            layers = layer_status()
            cache = cache_status()
            # Pass the same document (or an empty unavailable marker) to every
            # eligibility calculation; no helper may silently reread status.
            management_input = deployment_data if status_available else {}
            channel = channel_status(management_input)
            image = image_switch_status(management_input)
            delete = delete_deployment_status(management_input)
            rollback = rollback_status()
            cancel = self._maintenance_status_with_request("cancel", cancel)
            layers = self._maintenance_status_with_request("layers", layers)
            cache = self._maintenance_status_with_request("cache", cache)
            channel = self._maintenance_status_with_request("channel", channel)
            image = self._maintenance_status_with_request("image", image)
            delete = self._maintenance_status_with_request("delete", delete)
            rollback = self._maintenance_status_with_request("rollback", rollback)
            cancel = self._stable_maintenance_status("cancel", cancel)
            layers = self._stable_maintenance_status("layers", layers)
            cache = self._stable_maintenance_status("cache", cache)
            channel = self._stable_maintenance_status("channel", channel)
            image = self._stable_maintenance_status("image", image)
            delete = self._stable_maintenance_status("delete", delete)
            rollback = self._stable_maintenance_status("rollback", rollback)
            refresh_cache_target = self._cache_target_needs_refresh(
                deployment, cancel, layers, cache, management_input=management_input,
                maintenance_statuses={"channel": channel, "image": image,
                                      "delete": delete, "rollback": rollback})
            if refresh_cache_target:
                next_cache_target = download_cache_target(management_input)
                with self._lock:
                    self._cache_target = next_cache_target
                    self._cache_target_checked_at = time.monotonic()
            with self._lock:
                cache_target = dict(self._cache_target)
            cancel_output = maintenance_output(cancel)
            layer_output = maintenance_output(layers)
            cache_output = maintenance_output(cache)
            channel_output = (maintenance_output(channel)
                              if INVOCATION_ID.fullmatch(channel.get("invocation", ""))
                              else {"logs": [], "error": ""})
            image_output = (maintenance_output(image)
                            if INVOCATION_ID.fullmatch(image.get("invocation", ""))
                            else {"logs": [], "error": ""})
            delete_output = (maintenance_output(delete)
                            if INVOCATION_ID.fullmatch(delete.get("invocation", ""))
                            else {"logs": [], "error": ""})
            rollback_output = (maintenance_output(rollback)
                               if INVOCATION_ID.fullmatch(rollback.get("invocation", ""))
                               else {"logs": [], "error": ""})
            if discovery_error:
                error = (error + "\n" + discovery_error).strip()
            with self._lock:
                # Final replay rebuilds module state from the beginning. Keep
                # the last published view until the complete evidence is ready.
                if run and self._finish_run == run and run not in self._journal_finished:
                    return
                failures = list(self._failures)
                result = "running" if running else "unknown"
                if run and not running and run in self._journal_finished:
                    if self._journal_historical:
                        if self._uupd_terminal == "failed" or failures:
                            result = "failed"
                        elif self._uupd_terminal in {"success", "skipped"} and not self._journal_failed:
                            result = self._uupd_terminal
                        else:
                            result = "unknown"
                    else:
                        if (state.get("ExecMainStatus") != "0" or state.get("Result") != "success" or failures
                                or self._uupd_terminal == "failed"):
                            result = "failed"
                        elif self._journal_failed or (unit.startswith("uupd") and self._uupd_terminal not in {"success", "skipped"}):
                            # Systemd reports success, but unreadable final logs mean
                            # we cannot rule out a module-level partial failure.
                            result = "unknown"
                        else:
                            result = "skipped" if self._uupd_terminal == "skipped" else "success"
                self._finish_module_steps(result)
                if self._journal_failed:
                    error = (error + "\n" + self._journal_failed).strip()
                modules = module_steps(plan, self._modules.values(), result if run else "idle")
                maintenance_busy = running or any(
                    item["state"] == "running"
                    or item["state"] == "unknown" and not item.get("_execution_idle")
                    for item in (cancel, layers, cache, channel, image, delete, rollback))
                cache_reason = cache_target.get("reason", "") or (cache.get("error", "") if not cache["available"] else "")
                if maintenance_busy:
                    cache_reason = N_("Another system operation is running or its state could not be verified.")
                elif not cache_reason and not cache_target.get("available"):
                    cache_reason = "Cache target has not been checked."
                self._management_revision += 1
                management_summary = deployments_backend.summarize_deployments(deployment_data) if status_available else {
                    "known": False, "error": status_error, "engine": "", "entries": [],
                    "next_boot_same": False, "transaction": "",
                }
                known = bool(management_summary.get("known"))
                if not known and management_summary.get("error"):
                    error = management_summary["error"]
                management = {
                    "known": bool(management_summary.get("known")),
                    "error": management_summary.get("error", ""),
                    "revision": self._management_revision,
                    "deployment_status": deployment_data if status_available else {},
                    "summary": management_summary,
                    "cancel_target": cancel_pending_backend.target(deployment_data) if status_available else cancel_pending_backend.target(None),
                    "layers_target": reset_layers_backend.target(deployment_data) if status_available else reset_layers_backend.target(None),
                    "channel_status": channel,
                    "image_status": image,
                    "delete_status": delete,
                }
                self.snapshot = Snapshot(available=any(u.startswith("uupd") for u, s in loaded), running=running, operation="rollback" if "rollback" in unit else "update", service=unit, run=run, stage=self._stage, result=result, logs=list(self._lines), failures=failures, deployment=deployment, deployment_label=label, deployment_known=known, error=error, progress_percent=self._progress_percent, modules=modules, cancel_available=cancel["available"], cancel_unit=cancel["unit"], cancel_state=cancel["state"], cancel_error=cancel["error"], layer_available=layers["available"], layer_unit=layers["unit"], layer_state=layers["state"], layer_error=layers["error"], cache_available=bool(cache["available"] and cache_target.get("available") and not maintenance_busy), cache_unit=cache["unit"], cache_state=cache["state"], cache_error=cache["error"], cache_token=cache_target.get("token", ""), cache_reason=cache_reason, cache_identity=cache_target.get("identity", ""), cache_image_count=len(cache_target.get("refs", [])), management=management)
                snapshot = self.snapshot
                snapshot.module_plan = plan
                snapshot.cancel_invocation = cancel.get("invocation", "")
                snapshot.cancel_logs = cancel_output["logs"]
                snapshot.cancel_log_error = cancel_output["error"]
                snapshot.layer_invocation = layers.get("invocation", "")
                snapshot.layer_logs = layer_output["logs"]
                snapshot.layer_log_error = layer_output["error"]
                snapshot.cache_invocation = cache.get("invocation", "")
                snapshot.cache_logs = cache_output["logs"]
                snapshot.cache_log_error = cache_output["error"]
                snapshot.channel_available = bool(channel.get("available"))
                snapshot.channel_state = channel.get("state", "unknown")
                snapshot.channel_unit = channel.get("unit", "")
                snapshot.channel_invocation = channel.get("invocation", "")
                snapshot.channel_error = channel.get("error", "")
                snapshot.channel_logs = channel_output["logs"]
                snapshot.channel_log_error = channel_output["error"]
                snapshot.image_available = bool(image.get("available"))
                snapshot.image_state = image.get("state", "unknown")
                snapshot.image_unit = image.get("unit", "")
                snapshot.image_invocation = image.get("invocation", "")
                snapshot.image_error = image.get("error", "")
                snapshot.image_logs = image_output["logs"]
                snapshot.image_log_error = image_output["error"]
                snapshot.delete_available = bool(delete.get("available"))
                snapshot.delete_state = delete.get("state", "unknown")
                snapshot.delete_unit = delete.get("unit", "")
                snapshot.delete_invocation = delete.get("invocation", "")
                snapshot.delete_error = delete.get("error", "")
                snapshot.delete_logs = delete_output["logs"]
                snapshot.delete_log_error = delete_output["error"]
                snapshot.rollback_available = bool(rollback.get("available"))
                snapshot.rollback_state = rollback.get("state", "unknown")
                snapshot.rollback_unit = rollback.get("unit", "")
                snapshot.rollback_invocation = rollback.get("invocation", "")
                snapshot.rollback_error = rollback.get("error", "")
                snapshot.rollback_logs = rollback_output["logs"]
                snapshot.rollback_log_error = rollback_output["error"]
                snapshot.maintenance_generations = {
                    "cancel": cancel.get("_request_generation", 0),
                    "layer": layers.get("_request_generation", 0),
                    "cache": cache.get("_request_generation", 0),
                    "channel": channel.get("_request_generation", 0),
                    "image": image.get("_request_generation", 0),
                    "delete": delete.get("_request_generation", 0),
                    "rollback": rollback.get("_request_generation", 0),
                }
            self.scheduler.idle_add(self._emit, snapshot)
        except Exception as error:
            with self._lock:
                message = str(error)[:1024] or N_("Management status could not be refreshed.")
                self._management_revision += 1
                self.snapshot.deployment = ""
                self.snapshot.deployment_label = ""
                self.snapshot.deployment_known = False
                self.snapshot.error = message
                unknown_summary = {
                    "known": False, "error": message, "engine": "", "entries": [],
                    "next_boot_same": False, "transaction": "",
                }
                unavailable = {"available": False, "reason": message}
                self.snapshot.management = {
                    "known": False,
                    "error": message,
                    "revision": self._management_revision,
                    "deployment_status": {},
                    "summary": unknown_summary,
                    "cancel_target": dict(unavailable),
                    "layers_target": dict(unavailable),
                    "channel_status": dict(unavailable, state="unknown", unit="", invocation="", error=message),
                    "image_status": dict(unavailable, state="unknown", unit="", invocation="", error=message),
                    "delete_status": dict(unavailable, state="unknown", unit="", invocation="", error=message),
                }
            self.scheduler.idle_add(self._emit, self.snapshot)
        finally:
            with self._lock:
                self._checking = False
                recheck, self._recheck = self._recheck, False
            if recheck:
                self.scheduler.idle_add(self.refresh)

    def _publish_running_service(self, unit, run, plan):
        with self._lock:
            if self._finish_run == run:
                return
            snapshot = Snapshot(**self.snapshot.to_dict())
            snapshot.available = True
            snapshot.running = True
            snapshot.result = "running"
            snapshot.service, snapshot.run = unit, run
            snapshot.operation = "rollback" if "rollback" in unit else "update"
            snapshot.stage = self._stage or N_("Reading update progress…")
            snapshot.logs = list(self._lines)
            snapshot.failures = list(self._failures)
            snapshot.progress_percent = self._progress_percent
            snapshot.module_plan = plan
            snapshot.modules = module_steps(plan, self._modules.values(), "running")
            self.snapshot = snapshot
        self.scheduler.idle_add(self._emit, snapshot)

    def _emit(self, snapshot):
        if not self._closed:
            self.changed(snapshot)
        return False

    def _start_journal(self, run):
        if self._journal:
            self._journal.terminate()
        with self._lock:
            self._final_module_evidence = None
            self._journal_generation += 1
            generation = self._journal_generation
            self._journal_run = run
            self._journal_historical = False
            self._lines, self._line_bytes = deque(), 0
            self._failures, self._stage = [], ""
            self._journal_failed = ""
            self._modules, self._active_module = {}, ""
            self._progress_percent = None
            self._uupd_terminal = None
            self._unmapped_module_failure = False
        threading.Thread(target=self._read_journal, args=(run, True, generation), daemon=True).start()

    def _load_historical_journal(self, unit, run):
        with self._lock:
            self._final_module_evidence = None
            follower = self._journal
            self._journal = None
            self._journal_generation += 1
            generation = self._journal_generation
            self._journal_run, self._journal_unit = run, unit
            self._journal_historical = True
            self._lines, self._line_bytes = deque(), 0
            self._failures, self._stage = [], ""
            self._journal_failed = ""
            self._modules, self._active_module = {}, ""
            self._progress_percent, self._uupd_terminal = None, None
            self._unmapped_module_failure = False
        if follower and follower.poll() is None:
            follower.terminate()
        self._read_journal(run, False, generation)

    def _ingest(self, run, text, generation=None):
        text, timestamp = journal_message(text)
        stage, failures, lines = interpret([text])
        event = update_event(text)
        with self._lock:
            if (run != self._journal_run or self._closed
                    or (generation is not None and generation != self._journal_generation)):
                return
            if stage and not self._active_module:
                self._stage = stage
            self._failures = list(dict.fromkeys(self._failures + failures))[-100:]
            if event:
                self._apply_module_event(event)
            current = self._modules.get(self._active_module)
            packages = current.setdefault("packages", {}) if current else None
            progress = current.setdefault("batch_progress", {}) if current else {}
            detail = live_module_detail(text, self._active_module, packages, progress)
            if detail and current and current.get("status") == "running":
                current["detail"] = current["live_detail"] = detail
                item = progress.get("item")
                if item:
                    if item != current.get("progress_item"):
                        current["fraction"] = None
                    current["progress_item"] = item
            if current and current.get("status") == "running" and "fraction" in progress:
                current["fraction"] = progress["fraction"]
            update_trace.record(run, text, event, self._active_module, list(self._modules.values()))
            for line in lines:
                if timestamp:
                    line = "\n".join("[" + timestamp + "] " + part for part in line.splitlines())
                self._lines.append(line)
                self._line_bytes += len(line.encode("utf-8", errors="replace"))
            while len(self._lines) > MAX_LOG_LINES or self._line_bytes > MAX_LOG_BYTES:
                removed = self._lines.popleft()
                self._line_bytes -= len(removed.encode("utf-8", errors="replace"))
            if (not self._journal_historical and self._finish_run != run
                    and self.snapshot.running and self.snapshot.run == run):
                token = (run, self._journal_generation)
                if self._journal_publish_pending != token and hasattr(self.scheduler, "timeout_add"):
                    self._journal_publish_pending = token
                    self.scheduler.timeout_add(250, self._publish_journal, *token)
                if event and event["kind"] == "completed" and hasattr(self.scheduler, "idle_add"):
                    self.scheduler.idle_add(self._journal_recheck)

    def _journal_recheck(self):
        self.refresh()
        return False

    def _publish_journal(self, run, generation):
        """Publish coalesced live events without running any status commands."""
        with self._lock:
            if self._journal_publish_pending == (run, generation):
                self._journal_publish_pending = None
            if (self._closed or self._finish_run == run
                    or run != self._journal_run or generation != self._journal_generation
                    or run != self.snapshot.run or not self.snapshot.running):
                return False
            snapshot = Snapshot(**self.snapshot.to_dict())
            snapshot.logs = list(self._lines)
            snapshot.stage = self._stage
            snapshot.failures = list(self._failures)
            snapshot.progress_percent = self._progress_percent
            snapshot.modules = module_steps(snapshot.module_plan, self._modules.values(), "running")
            self.snapshot = snapshot
        self._emit(snapshot)
        return False

    def _apply_module_event(self, event):
        """Track only configured modules and state transitions present in UUPD."""
        kind = event["kind"]
        identity = event.get("id", "")
        if self._uupd_terminal == "skipped" and kind in {
                "configured", "updating", "module_completed", "failed", "command_error"}:
            # Actual module activity takes precedence over a contradictory skip.
            self._uupd_terminal, self._stage = None, ""
        if kind == "skipped":
            if not self._modules and not self._failures and self._uupd_terminal is None:
                self._uupd_terminal, self._stage = "skipped", event["detail"]
                self._progress_percent = None
        elif kind == "command_error":
            # Only explicitly scoped command failures belong to a module.
            current = self._modules.get(identity)
            if current and current.get("status") == "running":
                current.update(status="failed", error=event["error"], fraction=None)
                self._failures = list(dict.fromkeys(self._failures + [identity]))[-100:]
        elif kind == "configured":
            if identity not in self._modules and len(self._modules) >= MAX_MODULE_STEPS:
                return
            current = self._modules.get(identity, {})
            current.update({
                "id": identity,
                "name": event["name"],
                "status": "pending" if event["enabled"] else "skipped",
                "detail": event["detail"],
                "error": "",
                "fraction": None,
                "source": "journal",
            })
            self._modules[identity] = current
        elif kind == "updating":
            current = self._modules.get(identity)
            if current is None:
                # A real module progress event establishes observed activity
                # even when journal rotation removed its opening configuration.
                if identity not in {key for key, _, _ in UUPD_MODULES} or len(self._modules) >= MAX_MODULE_STEPS:
                    return
                current = {"id": identity, "name": event["name"], "status": "pending",
                           "detail": "", "error": "", "fraction": None, "source": "journal"}
                self._modules[identity] = current
            if current.get("status") == "failed" or current.get("terminal_result"):
                return  # Late heartbeats cannot undo a failure or explicit result.
            order = [key for key, _, _ in UUPD_MODULES]
            previous = self._modules.get(self._active_module)
            if identity != self._active_module and identity in order and self._active_module in order:
                if order.index(identity) < order.index(self._active_module):
                    return  # A late bootc reader message must not reactivate a finished step.
                if previous and previous["status"] == "running":
                    # Moving to another module is not a successful exit record.
                    # Keep it awaiting final verification if that record was lost.
                    previous["status"] = "finished"
                    previous["fraction"] = None
            self._stage = event["name"]
            current["status"] = "running"
            if identity == "flatpak" and event["detail"] and event["detail"] != current.get("phase"):
                current["phase"] = event["detail"]
                current["packages"] = {}
                current["batch_progress"] = {}
                current.pop("progress_item", None)
                current["fraction"] = None
                current.pop("live_detail", None)
            if event["detail"] and not current.get("live_detail"):
                current["detail"] = event["detail"]
            self._active_module = identity
            if identity not in ("flatpak", "brew") or event["fraction"] is not None:
                current["fraction"] = event["fraction"]
            if event["progress_percent"] is not None:
                self._progress_percent = event["progress_percent"]
        elif kind == "module_completed":
            current = self._modules.get(identity)
            if current is None:
                if len(self._modules) >= MAX_MODULE_STEPS:
                    return
                name = next(name for key, name, _ in UUPD_MODULES if key == identity)
                current = {"id": identity, "name": name, "status": "pending",
                           "detail": "", "error": "", "fraction": None, "source": "journal"}
                self._modules[identity] = current
            current["terminal_result"] = True
            if not event["success"]:
                current.update(status="failed", error=current.get("error") or event["error"]
                               or N_("This update module reported a failure."), fraction=None)
                self._failures = list(dict.fromkeys(self._failures + [identity]))[-100:]
            elif current["status"] != "failed":
                current.update(status="success", fraction=None)
            if self._active_module == identity:
                self._active_module = ""
        elif kind == "failed":
            current = self._modules.get(identity)
            if current is not None:
                current["status"] = "failed"
                current["error"] = event["error"] or N_("This update module reported a failure.")
                current["fraction"] = None
            else:
                self._unmapped_module_failure = True
            if self._active_module == identity:
                self._active_module = ""
        elif kind == "completed":
            self._uupd_terminal = "success" if event["success"] else "failed"
            # The terminal UUPD record settles module results immediately even
            # if the service is still delivering its final desktop notification.
            self._finish_module_steps(self._uupd_terminal)

    def _finish_module_steps(self, result):
        terminal_agrees = ((result == "success" and self._uupd_terminal == "success") or
                           (result == "failed" and self._uupd_terminal == "failed"))
        if terminal_agrees:
            # UUPD emits this only after every enabled module Update() returned
            # and its aggregate failures were inspected. module_fail identifies
            # the specific failures; other configured modules completed cleanly.
            for module in self._modules.values():
                if module["status"] in ("failed", "skipped"):
                    continue
                if result == "failed" and (self._unmapped_module_failure or not self._failures):
                    module["status"] = "unknown"
                else:
                    module["status"] = "success"
        elif result != "running":
            # A terminal systemd state without a readable, complete UUPD record
            # cannot establish which configured modules finished.
            for module in self._modules.values():
                if module["status"] in ("pending", "running", "finished", "success"):
                    module["status"] = "unknown"
        if result != "running":
            for module in self._modules.values():
                module["fraction"] = None
        if result != "running":
            self._active_module = ""

    def _read_journal(self, run, follow, generation=None):
        if generation is None:
            with self._lock:
                generation = self._journal_generation
        process = None
        try:
            with self._lock:
                if run != self._journal_run or generation != self._journal_generation or self._closed:
                    return
            argv = ["journalctl", "-b", "_SYSTEMD_INVOCATION_ID=" + run,
                    "--no-pager", "--quiet", "--all", "-o", "json"]
            if follow:
                # Make complete invocation replay explicit rather than rely
                # on journalctl's tail selection defaults in follow mode.
                argv.extend(["--follow", "--no-tail"])
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if follow:
                with self._lock:
                    if (run != self._journal_run or generation != self._journal_generation
                            or self._closed):
                        process.terminate()
                        return
                    self._journal = process
            with process.stdout:
                for text in update_journal.records(process.stdout):
                    if (self._closed or run != self._journal_run
                            or generation != self._journal_generation):
                        break
                    if text is None:
                        with self._lock:
                            if run == self._journal_run and generation == self._journal_generation:
                                self._journal_failed = "An oversized update log record could not be read; the full result could not be verified."
                        self._ingest(run, "[An overlong journal message was omitted]", generation)
                    else:
                        self._ingest(run, text, generation)
            code = process.wait(timeout=5)
            if code and code != -15:
                raise RuntimeError("Update logs are unavailable. Check permission to read the UUPD journal.")
            if not follow:
                with self._lock:
                    if run == self._journal_run and generation == self._journal_generation:
                        self._merge_final_module_evidence(run, generation)
                        self._journal_finished.add(run)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            with self._lock:
                if run == self._journal_run and generation == self._journal_generation:
                    self._journal_failed = str(error)
                    if not follow:
                        self._merge_final_module_evidence(run, generation)
                        self._journal_finished.add(run)
        finally:
            if process and process.poll() is None:
                process.terminate()

    def _merge_final_module_evidence(self, run, generation):
        """Reconcile this invocation before making its replay visible. Lock held."""
        evidence = self._final_module_evidence
        if (evidence is None or self._closed or run != self._journal_run
                or generation != self._journal_generation
                or (run, generation) != evidence["identity"]):
            return
        for identity, observed in evidence["modules"].items():
            replayed = self._modules.get(identity)
            if replayed is None or (observed["status"] == "failed" and replayed["status"] != "failed"):
                self._modules[identity] = observed
        self._failures = list(dict.fromkeys(evidence["failures"] + self._failures))[-100:]
        self._unmapped_module_failure |= evidence["unmapped_failure"]
        if "failed" in (evidence["terminal"], self._uupd_terminal):
            self._uupd_terminal = "failed"
        elif self._uupd_terminal is None:
            self._uupd_terminal = evidence["terminal"]
        if self._uupd_terminal == "skipped":
            # Preserve a witnessed preflight skip if its record rotated out,
            # but never let it override evidence of actual module execution.
            self._uupd_terminal = None if self._modules or self._failures else "skipped"
            self._stage = METERED_SKIP_REASON if self._uupd_terminal else ""
        self._final_module_evidence = None

    def _finish_journal(self, run):
        # A finite replay catches final messages after systemd announces completion.
        with self._lock:
            if self._closed or run != self._journal_run:
                return
            # Invalidate the follower before stopping it. Its reader may not
            # have created the process yet, or may already hold buffered lines.
            # Generation checks prevent either case from touching replay state.
            self._journal_generation += 1
            generation = self._journal_generation
            # Journald may evict early records during a long run. Preserve only
            # evidence actually observed for this invocation, never its plan.
            self._final_module_evidence = {
                "identity": (run, generation),
                "modules": {key: dict(module) for key, module in self._modules.items()},
                "failures": list(self._failures),
                "unmapped_failure": self._unmapped_module_failure,
                "terminal": self._uupd_terminal,
            }
            follower = self._journal
            self._journal = None
            self._lines, self._line_bytes = deque(), 0
            self._failures, self._stage = [], ""
            self._journal_failed = ""
            # Retain observed coverage so replayed progress/failures can still
            # be attributed when their opening configuration record was evicted.
            self._modules = {
                key: {"id": key, "name": module["name"],
                      "status": "skipped" if module["status"] == "skipped" else "pending",
                      "detail": module.get("detail", ""), "error": "",
                      "fraction": None, "source": "journal"}
                for key, module in self._final_module_evidence["modules"].items()
            }
            self._active_module = ""
            self._progress_percent, self._uupd_terminal = None, None
            self._unmapped_module_failure = False
        if follower and follower.poll() is None:
            follower.terminate()
        self._read_journal(run, False, generation)
        with self._lock:
            checking = self._checking
            if checking:
                self._recheck = True
        if not checking:
            self.refresh()

    def close(self):
        self._closed = True
        if self._journal and self._journal.poll() is None:
            self._journal.terminate()


def start_operation(operation):
    if _channel_service_is_running():
        raise RuntimeError(C_('{bazzite} is changing update channels. Wait for it to finish.', bazzite='Bazzite'))
    if _cancel_service_is_running():
        raise RuntimeError(N_("Pending-deployment cancellation is running. Wait for it to finish."))
    if _layer_service_is_running():
        raise RuntimeError(N_("Package-layer removal is running. Wait for it to finish."))
    if _cache_service_is_running():
        raise RuntimeError(N_("Download-cache cleanup is running. Wait for it to finish."))
    if _channel_service_is_running() or _image_switch_service_is_running() or _delete_service_is_running():
        raise RuntimeError(N_("Another deployment-management operation is running. Wait for it to finish."))
    active = [unit for unit in SERVICES if service_state(unit).get("ActiveState") in ("active", "activating", "deactivating", "reloading")]
    if active:
        if operation == "update" and any(unit.startswith("uupd") for unit in active):
            return "Attached to the running update."
        raise RuntimeError(N_("Another system operation is running. Wait for it to finish."))
    if operation in ("update", "rollback"):
        _require_no_active_rpm_ostree_transaction()
    unit = "uupd-manual.service" if operation == "update" else "bazzite-portal-maintenance@rollback.service"
    if operation == "rollback":
        _start_maintenance_unit("rollback", unit)
    else:
        command(["systemctl", "start", "--no-block", unit])
    return "Operation requested."


def _require_no_active_rpm_ostree_transaction():
    """Recheck authoritative rpm-ostree state immediately before starting a job."""
    raw = command(["rpm-ostree", "status", "--json"])
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="replace")) > maintenance_runtime.MAX_OUTPUT_BYTES:
        raise RuntimeError("Could not verify RPM-OSTree transaction state.")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError("Could not verify RPM-OSTree transaction state.") from error
    if (not isinstance(data, dict) or "transaction" not in data
            or not deployments_backend.summarize_deployments(data).get("known")):
        raise RuntimeError("Could not verify RPM-OSTree transaction state.")
    if maintenance_runtime.has_transaction(data["transaction"]):
        raise RuntimeError(C_('An {rpm_ostree} transaction is active. Wait for it to finish.', rpm_ostree='RPM-OSTree'))


def _cancel_service_is_running():
    return any(item.get("ActiveState") in CANCEL_ACTIVE_STATES for item in _cancel_instances())


def _cache_service_is_running():
    status = cache_status()
    if status["state"] == "unknown":
        raise RuntimeError(status["error"] or "Could not verify download-cache cleanup state.")
    return status["state"] == "running"


def start_cancel_rollback(token):
    """Restore the running deployment as default without deleting any version."""
    if not isinstance(token, str) or not rollback_cancel_backend.TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid rollback cancellation token.")
    data = json.loads(command(["rpm-ostree", "status", "--json"]))
    selected = rollback_cancel_backend.target(data)
    if not selected["available"] or selected["token"] != token:
        raise RuntimeError(selected["reason"] or "The scheduled rollback changed after confirmation.")
    if (_cancel_service_is_running() or _layer_service_is_running()
            or _cache_service_is_running() or _channel_service_is_running()
            or _image_switch_service_is_running() or _delete_service_is_running()):
        raise RuntimeError(N_("Another maintenance operation is running."))
    active = _managed_operation_active()
    if active:
        raise RuntimeError("Another system operation is running: {active}.".format(active=active))
    _start_maintenance_unit("rollback", f"bazzite-portal-maintenance@cancel-rollback-{token}.service")
    return "Rollback canceled."


def start_cancel_pending(token):
    if not isinstance(token, str) or not cancel_pending_backend.TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid cancellation token.")
    if not _cancel_template_exists():
        raise RuntimeError("Pending-deployment cancellation service is not installed.")
    data = json.loads(command(["rpm-ostree", "status", "--json"]))
    pending = cancel_pending_backend.target(data)
    if not pending["available"] or pending["token"] != token:
        raise RuntimeError(pending["reason"] or N_("The pending deployment changed after confirmation."))
    if _channel_service_is_running():
        raise RuntimeError(C_('{bazzite} is changing update channels. Wait for it to finish.', bazzite='Bazzite'))
    if _cancel_service_is_running():
        raise RuntimeError(N_("Pending-deployment cancellation is already running."))
    if _layer_service_is_running():
        raise RuntimeError(N_("Package-layer removal is already running."))
    if _cache_service_is_running():
        raise RuntimeError(N_("Download-cache cleanup is already running."))
    active = _managed_operation_active()
    if active:
        raise RuntimeError("Another system operation is running: {active}.".format(active=active))
    unit = f"bazzite-portal-maintenance@cancel-pending-{token}.service"
    _start_maintenance_unit("cancel", unit)
    return "Pending-deployment cancellation completed."


def _layer_service_is_running():
    status = layer_status()
    if status["state"] == "unknown":
        raise RuntimeError(status["error"] or "Could not verify package-layer reset state.")
    return status["state"] == "running"


def start_remove_layers(token):
    if not isinstance(token, str) or not reset_layers_backend.TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid package-layer removal token.")
    if not layer_status()["available"]:
        raise RuntimeError("Package-layer reset service is not installed.")
    data = json.loads(command(["rpm-ostree", "status", "--json"]))
    selected = reset_layers_backend.target(data)
    if not selected["available"] or selected["token"] != token:
        raise RuntimeError(selected["reason"] or N_("The requested package list changed after confirmation."))
    if _channel_service_is_running():
        raise RuntimeError(C_('{bazzite} is changing update channels. Wait for it to finish.', bazzite='Bazzite'))
    if _layer_service_is_running() or _cancel_service_is_running():
        raise RuntimeError(N_("A package-layer or pending-deployment operation is already running."))
    if _cache_service_is_running():
        raise RuntimeError(N_("Download-cache cleanup is already running."))
    active = _managed_operation_active()
    if active:
        raise RuntimeError("Another system operation is running: {active}.".format(active=active))
    _start_maintenance_unit("layers", f"bazzite-portal-maintenance@reset-layers-{token}.service")


def start_remove_selected_layers(token, packages):
    if not isinstance(token, str) or not reset_layers_backend.TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid package-layer removal token.")
    if not isinstance(packages, (list, tuple)) or not packages or any(not isinstance(item, str) for item in packages):
        raise ValueError(N_("Choose one or more requested packages to remove."))
    if len(set(packages)) != len(packages):
        raise ValueError("The selected package list contains duplicates.")
    status = layer_status()
    if not status.get("selected_available", False):
        raise RuntimeError(status["error"] or "Package-layer removal service is not installed.")
    data = json.loads(command(["rpm-ostree", "status", "--json"]))
    confirmed = reset_layers_backend.target(data)
    if not confirmed["available"] or confirmed["token"] != token:
        raise RuntimeError(confirmed["reason"] or N_("The requested package list changed after confirmation."))
    if any(package not in confirmed["packages"] for package in packages):
        raise ValueError("Every selected package must be in the confirmed request list.")
    if _channel_service_is_running():
        raise RuntimeError(C_('{bazzite} is changing update channels. Wait for it to finish.', bazzite='Bazzite'))
    if status["state"] == "running" or _cancel_service_is_running():
        raise RuntimeError(N_("A package-layer or pending-deployment operation is already running."))
    if _cache_service_is_running():
        raise RuntimeError(N_("Download-cache cleanup is already running."))
    active = _managed_operation_active()
    if active:
        raise RuntimeError("Another system operation is running: {active}.".format(active=active))
    indexes = {package: index for index, package in enumerate(confirmed["packages"])}
    bitmap = sum(1 << indexes[package] for package in packages)
    instance = f"{token}-{bitmap:064x}"
    _start_maintenance_unit("layers", f"bazzite-portal-maintenance@remove-selected-layers-{instance}.service")


def start_clear_download_cache(token):
    if not isinstance(token, str) or not cache_clear_backend.TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid download-cache token.")
    status = cache_status()
    if not status["available"]:
        raise RuntimeError(status["error"] or "Download-cache cleanup service is not installed.")
    selected = download_cache_target()
    if not selected["available"] or selected["token"] != token:
        raise RuntimeError(selected["reason"] or N_("The deployments or imported image references changed after confirmation."))
    if _channel_service_is_running():
        raise RuntimeError(C_('{bazzite} is changing update channels. Wait for it to finish.', bazzite='Bazzite'))
    if status["state"] == "running" or _cancel_service_is_running() or _layer_service_is_running():
        raise RuntimeError(N_('Another maintenance operation is running.'))
    active = _managed_operation_active()
    if active:
        raise RuntimeError("Another system operation is running: {active}.".format(active=active))
    _start_maintenance_unit("cache", f"bazzite-portal-maintenance@clear-download-cache-{token}.service")
    return "Download-cache cleanup completed. OSTree may retain shared objects used by other images."


def download_channel_target():
    """Read a token-bound channel target on a worker thread."""
    try:
        raw = command(["rpm-ostree", "status", "--json"])
        if not isinstance(raw, str) or len(raw.encode("utf-8", errors="replace")) > channel_switch_backend.MAX_STATUS_BYTES:
            raise ValueError("RPM-OSTree deployment status is oversized.")
        return channel_switch_backend.target(json.loads(raw),
                                             helper_available=_channel_switch_status()["available"])
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        result = channel_switch_backend.target(None)
        result["reason"] = str(error)[:1024]
        return result


def start_channel_switch(token):
    if _delete_service_is_running():
        raise RuntimeError(N_("Saved-deployment deletion is running. Wait for it to finish."))
    if _image_switch_service_is_running():
        raise RuntimeError(C_('{bazzite} is changing images. Wait for it to finish.', bazzite='Bazzite'))
    if not isinstance(token, str) or not channel_switch_backend.TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid channel-switch confirmation token.")
    status = channel_status()
    if status.get("state") == "unknown":
        raise RuntimeError(status.get("error") or "The previous channel-switch result could not be verified.")
    if status.get("state") == "running":
        raise RuntimeError(N_('A release channel rebase is already running.'))
    if not status["available"] or status["token"] != token:
        raise RuntimeError(status["reason"] or N_("The running deployment changed after confirmation."))
    if (_cancel_service_is_running() or _layer_service_is_running() or _cache_service_is_running()):
        raise RuntimeError(N_('Another maintenance operation is running.'))
    active = _managed_operation_active()
    if active:
        raise RuntimeError("Another system operation is running: {active}.".format(active=active))
    unit = f"bazzite-portal-maintenance@channel-switch-{token}.service"
    _start_maintenance_unit("channel", unit)
    return f'Bazzite is rebasing to the {status["target_track"]} channel. Restart to apply the staged deployment.'


def start_image_switch(token, image, tag):
    """Validate a fresh, confirmed image choice and request its fixed helper unit."""
    helper = image_switch_status()
    if helper.get("state") == "unknown":
        raise RuntimeError(helper.get("error") or "The previous image-switch result could not be verified.")
    if helper.get("state") == "running":
        raise RuntimeError(N_('An image rebase is already running.'))
    if not helper.get("available"):
        raise RuntimeError(helper.get("error") or "The image-switch service is not installed.")
    raw = command(["rpm-ostree", "status", "--json"])
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="replace")) > image_switch_backend.MAX_STATUS_BYTES:
        raise ValueError("RPM-OSTree deployment status is oversized.")
    try:
        deployment_status = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError("RPM-OSTree returned malformed deployment status.") from error
    request = image_switch_backend.prepare_request(
        deployment_status, image, tag, helper_available=helper["available"])
    if request["token"] != token:
        raise RuntimeError(N_("The running deployment changed after confirmation."))
    if _channel_service_is_running():
        raise RuntimeError(N_("Another deployment operation is already running."))
    if _cancel_service_is_running() or _layer_service_is_running() or _cache_service_is_running():
        raise RuntimeError(N_('Another maintenance operation is running.'))
    active = _managed_operation_active()
    if active:
        raise RuntimeError("Another system operation is running: {active}.".format(active=active))
    unit = f"bazzite-portal-maintenance@image-switch-{request['instance']}.service"
    _start_maintenance_unit("image", unit)
    return f'Bazzite is rebasing to {image}:{tag}. Restart to apply the staged deployment.'


def start_delete_deployment(token):
    if not isinstance(token, str) or not delete_deployments_backend.TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid deployment deletion token.")
    status = delete_deployment_status()
    if status.get("state") in {"unknown", "running"}:
        raise RuntimeError(status.get("error") or N_("Deployment deletion is already running or could not be verified."))
    if not status.get("available") or not any(item.get("token") == token for item in status["deployments"]):
        raise RuntimeError(status.get("reason") or N_("The deployment list changed after confirmation."))
    if (_channel_service_is_running() or _cancel_service_is_running()
            or _layer_service_is_running() or _cache_service_is_running()):
        raise RuntimeError(N_("Another maintenance operation is running."))
    active = _managed_operation_active()
    if active:
        raise RuntimeError("Another system operation is running: {active}.".format(active=active))
    _start_maintenance_unit("delete", f"bazzite-portal-maintenance@delete-deployment-{token}.service")
    return N_('Pinned deployment deleted')


def restart():
    if _channel_service_is_running():
        raise RuntimeError(C_('{bazzite} is changing update channels. Wait for it to finish.', bazzite='Bazzite'))
    if _cancel_service_is_running():
        raise RuntimeError(N_("Pending-deployment cancellation is running. Wait for it to finish."))
    if _layer_service_is_running():
        raise RuntimeError(N_("Package-layer removal is running. Wait for it to finish."))
    active = _managed_operation_active()
    if active:
        raise RuntimeError("Another system operation is running: {active}.".format(active=active))
    deployment, _, known, error = deployment_state()
    if not known or not deployment:
        raise RuntimeError(error or N_("There is no pending deployment to restart into."))
    if _cache_service_is_running():
        raise RuntimeError(N_("Download-cache cleanup is running. Wait for it to finish."))
    if _image_switch_service_is_running() or _delete_service_is_running():
        raise RuntimeError(N_("Another deployment-management operation is running. Wait for it to finish."))
    command(["systemctl", "reboot"])


def system_information():
    info = {}
    with open("/etc/os-release", encoding="utf-8") as stream:
        for line in stream:
            key, separator, value = line.strip().partition("=")
            if separator:
                info[key] = value.strip('"')
    for argv in (["rpm-ostree", "status", "--json"], ["bootc", "status", "--json"]):
        try:
            info["deployment_status"] = json.loads(command(argv))
            break
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            continue
    return info
