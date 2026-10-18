"""Pure state interpretation shared by the monitor, UI and tests."""
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import re

from bazzite_portal.core.i18n import C_, N_

SERVICES = ("uupd.service", "uupd-manual.service", "bazzite-portal-maintenance@rollback.service",
            "bazzite-portal-rollback.service")  # Read-only support for a running pre-upgrade rollback.
MAX_LOG_LINES = 1500
MAX_LOG_BYTES = 256 * 1024
MAX_MODULE_STEPS = 64
MAX_PACKAGE_ITEMS = 1024
UUPD_MODULES = (
    ("system", N_("System"), N_("Operating system")),
    ("brew", "Brew", N_("CLI Apps")),
    ("flatpak", "Flatpak", N_("System Apps")),
    ("distrobox", "Distrobox", C_("Rootful {distrobox} containers", distrobox="Distrobox")),
)
UUPD_MODULE_FIELDS = {
    "system": {"disable", "rpm-ostree-binary", "bootc-binary", "skopeo-binary"},
    "brew": {"disable", "prefix", "repository", "cellar", "path"},
    "flatpak": {"disable", "binary-path"},
    "distrobox": {"disable", "binary-path"},
}


@dataclass
class Snapshot:
    available: bool = False
    running: bool = False
    operation: str = "update"
    service: str = ""
    run: str = ""
    result_viewed_run: str = ""
    stage: str = ""
    result: str = "unknown"
    logs: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    deployment: str = ""
    deployment_label: str = ""
    deployment_known: bool = False
    error: str = ""
    # UUPD's `overall` field is run-wide. Module fractions are separate and are
    # populated only when UUPD reports supported per-module progress.
    progress_percent: float | None = None
    modules: list = field(default_factory=list)
    module_plan: list = field(default_factory=list)
    cancel_available: bool = False
    cancel_unit: str = ""
    cancel_state: str = "unavailable"
    cancel_error: str = ""
    layer_available: bool = False
    layer_state: str = "unavailable"
    layer_unit: str = ""
    layer_error: str = ""
    cancel_invocation: str = ""
    cancel_logs: list = field(default_factory=list)
    cancel_log_error: str = ""
    layer_invocation: str = ""
    layer_logs: list = field(default_factory=list)
    layer_log_error: str = ""
    cache_available: bool = False
    cache_unit: str = ""
    cache_state: str = "unavailable"
    cache_error: str = ""
    cache_token: str = ""
    cache_reason: str = ""
    cache_identity: str = ""
    cache_image_count: int = 0
    cache_invocation: str = ""
    cache_logs: list = field(default_factory=list)
    cache_log_error: str = ""
    channel_available: bool = False
    channel_state: str = "unavailable"
    channel_unit: str = ""
    channel_invocation: str = ""
    channel_error: str = ""
    channel_logs: list = field(default_factory=list)
    channel_log_error: str = ""
    image_available: bool = False
    image_state: str = "unavailable"
    image_unit: str = ""
    image_invocation: str = ""
    image_error: str = ""
    image_logs: list = field(default_factory=list)
    image_log_error: str = ""
    delete_available: bool = False
    delete_state: str = "unavailable"
    delete_unit: str = ""
    delete_invocation: str = ""
    delete_error: str = ""
    delete_logs: list = field(default_factory=list)
    delete_log_error: str = ""
    rollback_available: bool = False
    rollback_state: str = "unavailable"
    rollback_unit: str = ""
    rollback_invocation: str = ""
    rollback_error: str = ""
    rollback_logs: list = field(default_factory=list)
    rollback_log_error: str = ""
    maintenance_generations: dict = field(default_factory=dict)
    # One coherent view of deployment state and every action target derived
    # from that exact status read. Helper execution/log state remains separate.
    management: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def interpret(lines):
    stage, failures = "", []
    output = []
    for line in lines:
        line = line[:8192]
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):
            data = None
        if isinstance(data, dict):
            message = str(data.get("msg", ""))
            module = data.get("module_name") or data.get("module")
            if module:
                stage = str(module)
            if message == "module_fail":
                failures.append(str(module or N_("An update module failed")))
            output.append(message if message else line)
        else:
            output.append(line)
    output = output[-MAX_LOG_LINES:]
    size = 0
    bounded = []
    for line in reversed(output):
        size += len(line.encode("utf-8", errors="replace"))
        if size > MAX_LOG_BYTES:
            break
        bounded.append(line)
    return stage, list(dict.fromkeys(failures)), list(reversed(bounded))


def journal_message(line):
    """Unwrap a journal record and retain its real local wall-clock time."""
    try:
        record = json.loads(line)
    except (TypeError, ValueError, RecursionError):
        return line, ""
    if not isinstance(record, dict) or "MESSAGE" not in record:
        return line, ""
    message = record["MESSAGE"]
    if not isinstance(message, str):
        return "[Unsupported journal message omitted]", ""
    timestamp = record.get("__REALTIME_TIMESTAMP")
    stamp = ""
    if isinstance(timestamp, str) and re.fullmatch(r"[0-9]{1,20}", timestamp):
        try:
            stamp = datetime.fromtimestamp(int(timestamp) / 1_000_000).strftime("%H:%M:%S")
        except (OverflowError, OSError, ValueError):
            pass
    return message, stamp


def module_id(value):
    """Stable identifier for the module names emitted by UUPD."""
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _system_phase_progress(progress, fraction):
    """Monotonic stage estimate; only a terminal outcome completes the ring."""
    if progress is not None:
        progress["fraction"] = max(progress.get("fraction") or 0.0, min(fraction, 0.98))


def live_module_detail(line, active, packages=None, progress=None):
    """Read engine output; count completed items, never download percentages."""
    try:
        data = json.loads(line)
    except (TypeError, ValueError, RecursionError):
        return ""
    if not isinstance(data, dict) or (data.get("module") not in (None, active)):
        return ""
    message = data.get("msg", "")
    if not isinstance(message, str):
        return ""
    if active == "system" and data.get("module") == "system":
        structured = data.get("progress_struct")
        if not isinstance(structured, dict):
            try:
                structured = json.loads(message)
            except (ValueError, RecursionError):
                structured = None
        if isinstance(structured, dict):
            stage = {"pulling": N_("Downloading"), "importing": N_("Importing"), "staging": N_("Deploying")}.get(structured.get("task"))
            if not stage or structured.get("type") not in ("ProgressSteps", "ProgressBytes"):
                return ""
            # Pulling steps already count layers that need fetching, excluding
            # cached layers. Bytes events carry the same completed-layer counts.
            detail = N_("Downloading layers") if structured.get("task") == "pulling" else stage
            done, total = structured.get("steps"), structured.get("stepsTotal")
            if (structured.get("task") == "pulling" and type(done) is int and type(total) is int
                    and 0 <= done <= total and total > 0):
                detail = C_("Downloading layers · {current} of {total}", current=done, total=total)
            return detail
    # RunLog emits unscoped messages; the invocation reader and sequential
    # active module supply their scope. Explicit other-module records are rejected.
    message = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", message).strip()
    if active == "system":
        # rpm-ostree's DEBUG log is unscoped. Reserve the last part of the
        # ring for deployment assembly. These are stage estimates, not time
        # estimates: optional/cached stages can be skipped and never regress.
        chunk = re.fullmatch(
            r"\[([0-9]{1,6})/([0-9]{1,6})\] Fetching (?:ostree chunk|layer) "
            r"[0-9a-f]{7,64} \([^()\r\n]{1,64}\)\.\.\.\s?done",
            message,
        )
        if chunk:
            current, total = int(chunk[1]), int(chunk[2])
            if 0 < current <= total:
                _system_phase_progress(progress, 0.60 * current / total)
                return C_("Downloading layers · {current} of {total}", current=current, total=total)
            return ""
        stages = (
            (r"Checking out tree [0-9a-f]{7,64}\.\.\.done", N_("Checking out system tree"), 0.63),
            (r"Importing rpm-md\.\.\.done", N_("Importing package metadata"), 0.66),
            (r"Resolving dependencies\.\.\.done", N_("Resolving dependencies"), 0.69),
            (r"Checking out packages\.\.\.done", N_("Checking out packages"), 0.72),
            (r"Running systemd-sysusers\.\.\.done", N_("Running system user setup"), 0.75),
            (r"Running pre scripts\.\.\.done", N_("Running pre-install scripts"), 0.78),
            (r"Running post scripts\.\.\.done", N_("Running post-install scripts"), 0.81),
            (r"Running posttrans scripts\.\.\.done", N_("Running post-transaction scripts"), 0.84),
            (r"Writing rpmdb\.\.\.done", N_("Writing package database"), 0.87),
            (r"Writing OSTree commit\.\.\.done", N_("Finalizing system deployment"), 0.90),
            (r"Generating initramfs\.\.\.done", N_("Generating boot image"), 0.94),
            (r"Staging deployment\.\.\.done", N_("Preparing deployment for restart"), 0.98),
            (r"Migrating pkgcache\.\.\.done", N_("Migrating package cache"), 0.61),
        )
        for pattern, detail, fraction in stages:
            if re.fullmatch(pattern, message):
                _system_phase_progress(progress, fraction)
                return detail
        # Native OSTree pulls provide transient CLI text rather than bootc's
        # progress fd. Only label known phases; their local percentages are not
        # a trustworthy fraction of the entire system update.
        for prefix, detail in (
                (r"Receiving delta parts: [0-9]+/[0-9]+ ", N_("Downloading system changes")),
                (r"Receiving metadata objects: [0-9]+/\(estimating\) ", N_("Downloading system metadata")),
                (r"Receiving objects[;:] [0-9]+% \([0-9]+/[0-9]+\) ", N_("Downloading system objects")),
                (r"Writing objects: [0-9]+$", N_("Writing system objects")),
                (r"Scanning metadata: [0-9]+$", N_("Scanning system metadata"))):
            if re.match(prefix, message):
                _system_phase_progress(progress, 0.01)
                return detail
        return ""
    if active == "brew":
        if progress is not None:
            if message.startswith("BAZZITE_PORTAL_BREW_PLAN "):
                progress.clear()
                progress["fraction"] = None
                try:
                    plan = json.loads(message[len("BAZZITE_PORTAL_BREW_PLAN "):])
                except (ValueError, RecursionError):
                    return ""
                apps = plan.get("apps") if isinstance(plan, dict) else None
                if (isinstance(apps, list) and 0 < len(apps) <= MAX_PACKAGE_ITEMS
                        and all(isinstance(app, str) and len(app) <= 128
                                and re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_+@.-]*(?:/[a-zA-Z0-9_][a-zA-Z0-9_+@.-]*)*", app)
                                for app in apps)
                        and len(set(apps)) == len(apps)):
                    progress.update(total=len(apps), planned=list(apps), completed=[],
                                    collecting=False, fraction=0.0)
                return ""
            if re.fullmatch(r"==> Upgrading [0-9]+ dependents? of upgraded (?:formula|formulae):", message):
                # This work is outside the announced outdated-formula total.
                # A full ring for that earlier list must not remain on screen.
                progress.clear()
                progress.update(fraction=None, scope_unknown=True)
                return C_('Updating {homebrew} dependencies', homebrew='Homebrew')
            total = re.fullmatch(r"==> (?:Would upgrade|Upgrading) ([0-9]+) outdated packages?:?", message)
            if total:
                if progress.get("scope_unknown"):
                    return ""
                progress.clear()
                progress["fraction"] = None
                if 0 < int(total[1]) <= MAX_PACKAGE_ITEMS:
                    progress.update(total=int(total[1]), planned=[], completed=[], collecting=True)
                return ""
            count = progress.get("total")
            planned = progress.get("planned", [])
            # Homebrew prints its requested formula list immediately after the
            # announced total. Dependencies and fetched bottles are not this list.
            formula = re.fullmatch(r"([\w+@.-]+(?:/[\w+@.-]+)*)\s+\S+(?:\s+->\s+\S+)?(?:\s+\([^()\n]{1,64}\))?", message)
            if (formula and count and progress.get("collecting") and len(planned) < count
                    and formula[1] not in planned):
                planned.append(formula[1])
                if len(planned) == count:
                    progress["fraction"] = 0.0
                return ""
            upgrading = re.fullmatch(r"==> Upgrading ([\w+@.-]+(?:/[\w+@.-]+)*)", message)
            if upgrading:
                progress["collecting"] = False
                if count and len(planned) == count and upgrading[1] not in planned:
                    progress.clear()
                    progress.update(fraction=None, scope_unknown=True)
                    return C_("Upgrading {package}", package=upgrading[1])[:512]
            if upgrading and count and len(planned) == count and upgrading[1] in planned:
                progress["current"] = upgrading[1]
                started = progress.setdefault("started", [])
                if upgrading[1] not in started:
                    started.append(upgrading[1])
                ordinal = started.index(upgrading[1]) + 1
                progress["item"] = f"{ordinal}/{count}"
                progress["fraction"] = max(progress.get("fraction") or 0.0, (ordinal - 1) / count)
            installed = re.search(r"(?:^|\s)(?:[^\s]+/)?Cellar/([^/\s]+)/[^/\s]+:\s+[0-9,]+ files?", message)
            current = progress.get("current")
            if (installed and count and current in planned
                    and installed[1] == current.rsplit("/", 1)[-1]
                    and current not in progress["completed"]):
                progress["completed"].append(current)
                progress["fraction"] = max(progress.get("fraction") or 0.0,
                                           (progress["started"].index(current) + 1) / count)
            if current and progress.get("item"):
                return f"{progress['item']} - {current}"[:512]
        if re.fullmatch(r"==> Updating Homebrew(?:\.{3}|…)?", message):
            return C_('Updating {homebrew}', homebrew='Homebrew')
        match = re.match(r"==> (?:Upgrading|Installing|Pouring|Fetching) (.+)", message)
        return message[4:512] if match else ""
    if active == "flatpak":
        completion_messages = {
            "Changes complete.", "Installation complete.",
            "Uninstall complete.", "Updates complete.",
        }
        if message in completion_messages:
            if progress is not None:
                # This ends one installation transaction, but UUPD's eventual
                # command result remains the authority for success or failure.
                if progress.get("fraction") is not None:
                    progress["fraction"] = 1.0
                progress["completed_batch"] = True
            return ""
        # Flatpak's table is tab-separated and can include empty columns, but
        # older output uses ordinary whitespace-delimited fields.
        table = re.match(r"\s*([0-9]+)\.\s*(.*)$", message)
        if table and table[1] == "1" and progress is not None and progress.pop("completed_batch", False):
            if packages is not None:
                packages.clear()
            # Keep the previous transaction's displayed 100% through the
            # table. The next operation event starts the new transaction.
            progress.clear()
        if table and packages is not None and len(packages) < MAX_PACKAGE_ITEMS:
            fields = table[2].split("\t") if "\t" in table[2] else table[2].split()
            app_id = next((field.strip() for field in fields
                           if re.fullmatch(r"[\w-]+(?:\.[\w-]+){2,}", field.strip())), None)
            if app_id:
                packages[table[1]] = app_id
        item = re.match(r"(Updating|Installing|Uninstalling|Downloading)\s+([0-9]+)/([0-9]+)", message)
        if item and 0 < int(item[2]) <= int(item[3]):
            app_id = packages.get(item[2]) if packages else None
            explicit_app = re.match(r"[^\w\s]+\s+([\w.-]+(?:\.[\w.-]+)+)", message[item.end():])
            if app_id:
                detail = f"{item[2]}/{item[3]} - {app_id}"
            elif explicit_app:
                detail = f"{item[2]}/{item[3]} - {explicit_app[1]}"
            else:
                detail = C_("{current} of {total}", current=item[2], total=item[3])
            if progress is not None:
                progress["item"] = f"{item[2]}/{item[3]}"
                # Even 100% is only download progress. Moving to the next ref
                # counts prior processed operations; success still comes from UUPD.
                progress["fraction"] = (max(progress.get("fraction") or 0.0,
                                            (int(item[2]) - 1) / int(item[3]))
                                        if int(item[3]) <= MAX_PACKAGE_ITEMS else None)
            return detail[:512]
        # Flatpak omits N/M when its CLI transaction handles a single ref.
        # Only a native operation ellipsis plus the exact one-row table
        # establishes a trustworthy 1/1 item; a generic UUPD heartbeat does not.
        single_item = re.match(r"^(Updating|Installing|Uninstalling)(?:…|\.\.\.)(.*)$", message)
        if single_item and isinstance(packages, dict) and set(packages) == {"1"}:
            app_id = packages["1"]
            if re.fullmatch(r"[\w-]+(?:\.[\w-]+){2,}", app_id):
                if progress is not None:
                    progress["item"] = "1/1"
                    progress["fraction"] = progress.get("fraction") or 0.0
                return f"1/1 - {app_id}"
        if re.match(r"(?:Updating|Installing|Uninstalling|Downloading)\s+(?:[0-9]+/[0-9]+[^\w\s]*\s+)?[\w.-]+", message):
            return message[:512]
    return ""


def update_event(line):
    """Decode only the UUPD JSON fields whose meaning is established by UUPD."""
    try:
        data = json.loads(line)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None

    msg = data.get("msg")
    if msg == "Failed checking for updates":
        return {
            "kind": "check_failed",
            "id": "system",
            "error": _event_error(data.get("error")) or C_('{uupd} could not check for system updates.', uupd='UUPD'),
        }
    if msg in ("Error occurred starting external command", "Error occurred while waiting for external command"):
        module = data.get("module")
        return {
            "kind": "command_error",
            "id": module_id(module) if isinstance(module, str) else "",
            "error": _event_error(data.get("error")) or msg,
        }
    if (msg in ("Executed update check", "Executed skopeo fallback check") and data.get("module") == "system"
            and isinstance(data.get("update"), bool)):
        return {"kind": "configured", "id": "system", "name": "System",
                "enabled": data["update"],
                "detail": N_("Operating system") if data["update"] else N_("No system update is available.")}
    module_name = data.get("module_name")
    configuration = data.get("module_configuration")
    if isinstance(configuration, dict) and isinstance(module_name, str) and module_name:
        enabled = configuration.get("Enabled")
        if isinstance(enabled, bool):
            return {
                "kind": "configured",
                "id": module_id(module_name),
                "name": str(configuration.get("Title") or module_name)[:256],
                "enabled": enabled,
                "detail": str(configuration.get("Description") or "")[:512],
            }

    if msg == "Updating":
        title = data.get("title") or module_name
        if not isinstance(title, str) or not title:
            return None
        overall = data.get("overall")
        if isinstance(overall, bool) or not isinstance(overall, (int, float)) or not 0 <= overall <= 100:
            overall = None
        description = data.get("description")
        if title == "System" and isinstance(description, str) and description.casefold() in ("bootc", "rpm-ostree"):
            description = N_("Preparing system update")
        step_progress = data.get("step_progress")
        # uupd's source documents item-level StepPercent reporting only for
        # System/bootc. Non-System module trackers remain at their default 0.
        module_fraction = None
        if title == "System" and isinstance(step_progress, (int, float)) and not isinstance(step_progress, bool):
            if 0 < step_progress <= 100:
                module_fraction = float(step_progress) / 100.0
        return {
            "kind": "updating",
            "id": module_id(title),
            "name": title[:256],
            "detail": str(description)[:512] if isinstance(description, str) else "",
            "progress_percent": float(overall) if overall is not None else None,
            "fraction": module_fraction,
        }

    if msg == "module_fail":
        failed = data.get("module") or module_name
        identity = module_id(failed)
        identity = {"cli-apps": "brew", "brew-update": "brew", "brew-upgrade": "brew",
                    "system-apps": "flatpak", "system-update": "system",
                    "rootful-distroboxes": "distrobox"}.get(identity, identity)
        if identity.startswith("apps-for-user-"):
            identity = "flatpak"
        elif identity.startswith("distroboxes-for-user-"):
            identity = "distrobox"
        output = data.get("output")
        output_error = ""
        if isinstance(output, dict):
            output_error = (_event_error(output.get("Stderr"))
                            or _event_error(output.get("Stdout"), tail=True))
        return {
            "kind": "failed",
            "id": identity,
            "name": str(failed or N_("An update module failed"))[:256],
            "error": _event_error(data.get("error")) or output_error,
        }
    if msg in ("Updates Completed Successfully", "Updates finished with errors!"):
        return {"kind": "completed", "success": msg == "Updates Completed Successfully"}
    return None


def _event_error(value, tail=False):
    """Return a bounded human-readable string only for a string error field."""
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value[-512:] if tail else value[:512]


def planned_modules(config_text=None):
    """Return a config-only UUPD plan; it does not predict runtime eligibility."""
    if config_text is None:
        data = {}
    else:
        try:
            data = json.loads(config_text)
        except (TypeError, ValueError):
            return []
        if not isinstance(data, dict) or set(data) - {"modules", "checks"}:
            return []

    modules = data.get("modules", {})
    if not isinstance(modules, dict) or set(modules) - {key for key, _, _ in UUPD_MODULES}:
        return []

    rows = []
    for key, name, detail in UUPD_MODULES:
        section = modules.get(key, {})
        if not isinstance(section, dict) or set(section) - UUPD_MODULE_FIELDS[key]:
            return []
        disable = section.get("disable", False)
        if not isinstance(disable, bool):
            status = "unknown"
            state_detail = "UUPD configuration does not provide a valid enabled state."
        elif disable:
            status = "skipped"
            state_detail = C_('Disabled in {uupd} configuration.', uupd='UUPD')
        else:
            status = "pending"
            state_detail = C_('Enabled by {uupd} configuration; runtime eligibility is not checked.', uupd='UUPD')
        rows.append({
            "id": key,
            "name": name,
            "status": status,
            "detail": detail + ". " + state_detail,
            "error": "",
            "fraction": None,
            "source": "config",
        })
    return rows


def module_steps(plan, observed, result):
    """Keep configured steps in place without inventing missing run results."""
    remaining = {row["id"]: dict(row) for row in observed}
    rows = []
    for planned in plan:
        row = remaining.pop(planned["id"], None)
        if row is None:
            row = dict(planned)
            if result in ("success", "failed", "unknown") and row["status"] == "pending":
                row["status"] = "unknown"
                row["detail"] = C_('{uupd} has not reported a result for this step.', uupd='UUPD')
        rows.append(row)
    rows.extend(remaining.values())
    return rows[:MAX_MODULE_STEPS]


def pending_deployment(data):
    """OSTree's first deployment is the next boot target; booted is explicit."""
    deployments = data.get("deployments")
    if isinstance(deployments, list) and deployments:
        first = deployments[0]
        if isinstance(first, dict) and first.get("booted") is False:
            identity = first.get("checksum") or first.get("id")
            if identity:
                return str(identity), str(first.get("version") or first.get("container-image-reference") or identity)
        return "", ""
    # bootc status --json records a staged image explicitly.
    status = data.get("status", {})
    staged = status.get("staged") if isinstance(status, dict) else None
    if isinstance(staged, dict):
        image = staged.get("image", {})
        if isinstance(image, dict):
            ostree = staged.get("ostree")
            image_reference = image.get("image")
            identity = image.get("imageDigest") or (ostree.get("checksum") if isinstance(ostree, dict) else None)
            if identity:
                return str(identity), str(image.get("version") or (image_reference.get("image") if isinstance(image_reference, dict) else None) or identity)
    return "", ""


def indicator(snapshot, preferences, dismissed, window_visible):
    if not preferences.get("show_indicator", True):
        return ""
    if snapshot.running:
        return "" if dismissed.get("run") == snapshot.run else "updating"
    if (not snapshot.running and snapshot.result == "failed" and snapshot.run
            and not window_visible and dismissed.get("run") != snapshot.run):
        return "failed"
    if snapshot.deployment and dismissed.get("deployment") != snapshot.deployment:
        return "restart"
    return ""
