"""Installed launcher and source-tree entry point."""
import argparse
import sys
from bazzite_portal.integration.tools import DEFAULT_CONFIG


def main():
    parser = argparse.ArgumentParser(description="Bazzite tools and system updates")
    parser.add_argument("config", nargs="?", default=DEFAULT_CONFIG)
    parser.add_argument("--preview", metavar="YAML", help="Harmless preview with all commands disabled")
    parser.add_argument("--updates", action="store_true")
    parser.add_argument("--action-id")
    parser.add_argument("--agent", action="store_true", help="Run the lightweight session monitor")
    parser.add_argument("--session-start", action="store_true", help="Open once at first graphical login, then follow the startup preference")
    args = parser.parse_args()
    if args.session_start and not args.agent:
        parser.error("--session-start requires --agent")
    if args.agent:
        from bazzite_portal.core.logging import setup_logging
        log = setup_logging("agent")
        log.info("Session agent starting")
        from bazzite_portal.services.agent import Agent
        Agent(session_start=args.session_start).run()
        log.info("Session agent stopped")
        return 0
    if not args.preview:
        from bazzite_portal.core.logging import setup_logging
        log = setup_logging("gui")
        log.info("Application starting")
    from bazzite_portal.ui.application import Application
    app = Application(args.preview or args.config, bool(args.preview), args.updates, args.action_id)
    if args.preview:
        from bazzite_portal.core.logging import setup_logging
        log = setup_logging("preview", base=app.preview_base)
        log.info("Preview starting")
    result = app.run(sys.argv)
    log.info("Application stopped (exit %s)", result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
