"""Generate RPM and AppStream metadata from the version in pyproject.toml."""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def prepare():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("Use an X.Y.Z version in pyproject.toml.")
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    date = (datetime.fromtimestamp(int(epoch), timezone.utc) if epoch
            else datetime.now(timezone.utc)).date().isoformat()
    output = ROOT / "build/packaging"
    output.mkdir(parents=True, exist_ok=True)
    for name, target in (
        ("bazzite-portal.spec", "bazzite-portal.spec"),
        ("io.github.Xarishark.bazzite_portal.metainfo.xml",
         "io.github.Xarishark.bazzite_portal.metainfo.xml.in"),
    ):
        text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
        if "@VERSION@" not in text:
            raise ValueError(f"Missing version placeholder in {name}.")
        (output / target).write_text(text.replace("@VERSION@", version).replace("@DATE@", date),
                                     encoding="utf-8", newline="\n")
    return version


if __name__ == "__main__":
    print(prepare())
