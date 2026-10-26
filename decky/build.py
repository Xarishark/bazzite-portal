"""Package the built plugin; never includes tests or installs into Decky Loader."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def package(output, wheel=None, *, revision=None, dirty=None, source_sha256=None):
    manifest = json.loads((HERE / "package.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if not isinstance(version, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("Use an X.Y.Z version in decky/package.json.")
    revision = revision or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    source_dirty = (bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT))
                    if dirty is None else dirty)
    if not (HERE / "dist/index.js").is_file():
        raise RuntimeError("Run pnpm build in decky/ first.")
    files = {"main.py": (HERE / "main.py").read_bytes(),
             "dist/index.js": (HERE / "dist/index.js").read_bytes(),
             "THIRD_PARTY_LICENSES.txt": (HERE / "dist/THIRD_PARTY_LICENSES.txt").read_bytes(),
             "plugin.json": (HERE / "plugin.json").read_bytes(),
             "LICENSE": (ROOT / "LICENSE").read_bytes(),
             "NOTICE.md": (HERE / "NOTICE.md").read_bytes()}
    files["licenses/Apache-2.0.txt"] = (ROOT / "LICENSES/Apache-2.0.txt").read_bytes()
    for name in ("cover.png", "landscape.png", "hero.png", "logo.png"):
        files["dist/assets/steam/" + name] = (ROOT / "bazzite_portal/assets/steam" / name).read_bytes()
    for folder in ("backend", "licenses"):
        for path in (HERE / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
                files[path.relative_to(HERE).as_posix()] = path.read_bytes()
    files["package.json"] = json.dumps({"name": manifest["name"], "version": version, "type": manifest["type"],
        "description": manifest["description"], "license": manifest["license"]}, indent=2).encode()
    with tempfile.TemporaryDirectory(prefix="portal-decky-wheel-", dir=HERE / "dist") as temporary:
        if wheel:
            candidate = Path(wheel)
        else:
            subprocess.run([sys.executable, "-m", "pip", "download", "--require-hashes", "--only-binary=:all:",
                "--no-deps", "-r", str(HERE / "requirements.txt"), "-d", temporary], check=True)
            candidate, = Path(temporary).glob("*.whl")
        expected = (HERE / "requirements.txt").read_text().split("sha256:", 1)[1].split()[0]
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
            raise RuntimeError("The bundled D-Bus dependency does not match its pinned hash.")
        with zipfile.ZipFile(candidate) as dependency:
            for name in dependency.namelist():
                if name.startswith("jeepney/") and name.endswith(".py") and "/tests/" not in name:
                    files["vendor/" + name] = dependency.read(name)
    files["build-info.json"] = json.dumps({"version": version, "revision": revision, "source_sha256": source_sha256,
        "dirty": source_dirty, "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}, indent=2).encode()
    output = Path(output) if output else HERE / "dist" / f"bazzite-portal-{version}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo("bazzite-portal/" + name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--wheel", help="Use an already downloaded, hash-checked Jeepney wheel")
    parser.add_argument("--revision", help="Exact revision supplied by source preparation")
    parser.add_argument("--source-sha256", help="Source-tree fingerprint supplied by source preparation")
    provenance = parser.add_mutually_exclusive_group()
    provenance.add_argument("--clean", dest="dirty", action="store_false")
    provenance.add_argument("--dirty", dest="dirty", action="store_true")
    parser.set_defaults(dirty=None)
    args = parser.parse_args()
    package(args.output, args.wheel, revision=args.revision, dirty=args.dirty, source_sha256=args.source_sha256)
