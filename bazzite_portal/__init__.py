"""Bazzite Portal: desktop tools and reconnectable system updates."""
APP_ID = "io.github.Xarishark.bazzite_portal"
from importlib.metadata import version
from pathlib import Path
import tomllib

_project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
if _project_file.is_file():
    VERSION = tomllib.loads(_project_file.read_text(encoding="utf-8"))["project"]["version"]
else:
    VERSION = version("bazzite-portal")
