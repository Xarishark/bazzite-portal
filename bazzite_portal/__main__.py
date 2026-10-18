"""Installed launcher and source-tree entry point."""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ""):
    # The installed launcher executes this file directly with -I, so Python
    # does not add the application root to sys.path for package imports.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bazzite_portal.integration.tools import DEFAULT_CONFIG


def _main():
    parser = argparse.ArgumentParser(description="Bazzite tools and system updates")
    parser.add_argument("config", nargs="?", default=DEFAULT_CONFIG)
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
    from bazzite_portal.core.logging import setup_logging
    log = setup_logging("gui")
    log.info("Application starting")
    from bazzite_portal.ui.application import Application
    app = Application(args.config, updates=args.updates, action_id=args.action_id)
    result = app.run(sys.argv)
    log.info("Application stopped (exit %s)", result)
    return result


def main():
    try:
        return _main()
    except KeyboardInterrupt:
        import logging
        logging.getLogger("bazzite_portal").info("Application interrupted; exiting")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
