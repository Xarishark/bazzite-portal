"""English presentation text; keep machine identifiers out of user messages.

Gettext integration is intentionally deferred. Reuse Portal's full wording where
the meaning matches, and keep placeholders intact for later catalog extraction.
"""

NOT_INSTALLED = "Bazzite Portal is not installed."
UNAVAILABLE = "The Bazzite Portal background worker is not running. Open Bazzite Portal or reconnect to try again."
CONNECTION_LOST = "The connection to Bazzite Portal was lost. Reconnect to read the current operation status."
BUSY = "Another system operation is running. Wait for it to finish."
INVALID_ACTION = "This action is unavailable."
CHANGED = "Deployment information changed. Review it again."
CACHE_CHANGED = "The download cache changed. Review the updated details and confirm again."
REQUEST_FAILED = "The operation request failed. Open Logs for details."
REQUEST_UNCERTAIN = "The connection was lost after submitting the request. The operation may still be running. Reconnect to check its status."
NORMAL_USER = "Bazzite Portal must run as the logged-in user."
BUS_UNAVAILABLE = "The user session bus is unavailable."
MALFORMED_SNAPSHOT = "The update monitor returned an invalid snapshot."

# These mirror the pure target-description errors in maintenance/rollback_cancel.py.
DEPLOYMENT_UNAVAILABLE = "RPM-OSTree deployment data is unavailable."
TRANSACTION_ACTIVE = "An RPM-OSTree transaction is active."
DEPLOYMENT_LIST_INVALID = "RPM-OSTree deployment list is missing, empty, or too large."
DEPLOYMENT_LIST_MALFORMED = "RPM-OSTree deployment list is malformed."
DEPLOYMENT_IDENTITY_INVALID = "A deployment identity or state is incomplete or malformed."
DEPLOYMENT_AMBIGUOUS = "Deployment identities are ambiguous."
STAGED_DEPLOYMENT = "A staged deployment must be resolved before canceling rollback."
BOOTED_INVALID = "Expected exactly one running deployment."
IDENTITY_TOO_LARGE = "Deployment identity data is too large to confirm safely."
NO_ROLLBACK = "No rollback is scheduled."
DIFFERENT_STATEROOTS = "The selected and running deployments belong to different stateroots."
UNKNOWN = "Unknown"
INSTALLER_UNAVAILABLE = "Could not check the bundled Decky plugin. Open Bazzite Portal to manage it."
INSTALLER_CHANGED = "The bundled plugin changed. Refresh its status and try again."

STEAM_FAILED = "Could not add Bazzite Portal to Steam. Open Logs for details."
AUTO_UPDATES_UNAVAILABLE = "Could not read or change automatic update scheduling."
