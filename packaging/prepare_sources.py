"""Build one coherent Portal source tarball and Decky ZIP for GitHub and COPR.

Node/pnpm/network are used only here, before creating the SRPM. Binary RPM builds
consume the prepared ZIP offline. Production snapshots an exact clean Git commit;
--worktree is an explicit, dirty local-review build and is never a release input.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = "24.22.1"
PNPM_VERSION = "11.25.0"
# Official https://nodejs.org/dist/v24.22.1/SHASUMS256.txt
NODE_ARCHIVES = {
    ("Linux", "x86_64"): ("linux-x64.tar.xz", "6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6"),
    ("Linux", "aarch64"): ("linux-arm64.tar.xz", "dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e"),
    ("Windows", "AMD64"): ("win-x64.zip", "d2046d28c2c3dcd211ac5145c0bf30e2047ef9dfb306693c8c6e2eccd8b95fa5"),
}
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def git(*args, root=ROOT):
    return subprocess.check_output(["git", "-c", f"safe.directory={root}", *args], cwd=root)


def snapshot(root=ROOT, revision="HEAD", worktree=False):
    commit = git("rev-parse", "--verify", revision + "^{commit}", root=root).decode().strip()
    epoch = int(git("show", "-s", "--format=%ct", commit, root=root))
    files = {}
    if worktree:
        if revision != "HEAD":
            raise ValueError("--worktree cannot be combined with another revision.")
        names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", root=root).decode().split("\0")
        for name in sorted(set(names) - {""}):
            path = root / name
            if path.is_symlink():
                raise ValueError("Source preparation does not accept symlinks: " + name)
            if path.is_file():
                files[name] = (path.read_bytes(), 0o755 if os.access(path, os.X_OK) and os.name != "nt" else 0o644)
    else:
        if git("status", "--porcelain", "--untracked-files=normal", root=root).strip():
            raise ValueError("Production source preparation requires a clean checkout; use --worktree for local review.")
        with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", commit, root=root))) as archive:
            for entry in archive:
                if entry.isdir():
                    continue
                if not entry.isfile():
                    raise ValueError("Source preparation does not accept links: " + entry.name)
                files[entry.name] = (archive.extractfile(entry).read(), entry.mode)
    version = tomllib.loads(files["pyproject.toml"][0].decode("utf-8"))["project"]["version"]
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError("Use an X.Y.Z version in pyproject.toml.")
    plugin_version = json.loads(files["decky/package.json"][0])["version"]
    if not isinstance(plugin_version, str) or not VERSION_PATTERN.fullmatch(plugin_version):
        raise ValueError("Use an X.Y.Z version in decky/package.json.")
    digest = hashlib.sha256()
    for name, (data, mode) in sorted(files.items()):
        digest.update(name.encode() + b"\0" + str(mode).encode() + b"\0" + data + b"\0")
    provenance = {"version": version, "plugin_version": plugin_version, "revision": commit, "dirty": worktree,
                  "source_sha256": digest.hexdigest()}
    return files, provenance, epoch


def write_tar(path, files, prefix, epoch):
    with path.open("wb") as output, gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=epoch) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, (data, mode) in sorted(files.items()):
                entry = tarfile.TarInfo(prefix + "/" + name)
                entry.size, entry.mode, entry.mtime = len(data), mode, epoch
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = ""
                archive.addfile(entry, io.BytesIO(data))


def node_runtime(directory):
    existing = shutil.which("node")
    if existing and subprocess.check_output([existing, "--version"]).decode().strip() == "v" + NODE_VERSION:
        return Path(existing)
    try:
        suffix, expected = NODE_ARCHIVES[(platform.system(), platform.machine())]
    except KeyError as error:
        raise RuntimeError("No pinned Node source-build runtime for this platform.") from error
    name = f"node-v{NODE_VERSION}-{suffix}"
    with urllib.request.urlopen(f"https://nodejs.org/dist/v{NODE_VERSION}/{name}", timeout=60) as response:
        data = response.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("The Node source-build runtime failed checksum verification.")
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(directory)
        return directory / name.removesuffix(".zip") / "node.exe"
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(directory, filter="data")
    return directory / name.removesuffix(".tar.xz") / "bin/node"


def verify_zip(path, version, revision=None, source_sha256=None, require_clean=False):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(not n.startswith("bazzite-portal/") or ".." in Path(n).parts for n in names):
            raise ValueError("The plugin ZIP has invalid or duplicate paths.")
        metadata = json.loads(archive.read("bazzite-portal/build-info.json"))
        package = json.loads(archive.read("bazzite-portal/package.json"))
        if package.get("type") != "module":
            raise ValueError("The plugin must use Decky's ES module loader.")
        if metadata.get("version") != version or package.get("version") != version:
            raise ValueError("The plugin does not match the selected plugin version.")
        if revision is not None and metadata.get("revision") != revision:
            raise ValueError("The plugin does not match the selected source commit.")
        if source_sha256 is not None and metadata.get("source_sha256") != source_sha256:
            raise ValueError("The plugin does not match the selected source tree.")
        if require_clean and metadata.get("dirty") is not False:
            raise ValueError("A dirty local plugin build cannot be released.")
        recorded = metadata.get("files")
        if not isinstance(recorded, dict) or set(names) != {"bazzite-portal/build-info.json", *("bazzite-portal/" + n for n in recorded)}:
            raise ValueError("The plugin ZIP manifest does not cover its payload.")
        for name, digest in recorded.items():
            if hashlib.sha256(archive.read("bazzite-portal/" + name)).hexdigest() != digest:
                raise ValueError("The plugin ZIP manifest checksum is invalid.")
    return metadata


def prepare(output, *, root=ROOT, revision="HEAD", worktree=False, wheel=None):
    files, provenance, epoch = snapshot(root, revision, worktree)
    output = Path(output).resolve()
    (output / "SOURCES").mkdir(parents=True, exist_ok=True)
    (output / "SPECS").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="portal-source-", dir=output) as temporary:
        stage = Path(temporary)
        for name, (data, mode) in files.items():
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(mode)
        node = node_runtime(stage / ".node")
        env = {**os.environ, "PATH": str(node.parent) + os.pathsep + os.environ.get("PATH", ""),
               "SOURCE_DATE_EPOCH": str(epoch), "CI": "true", "TZ": "UTC", "LC_ALL": "C.UTF-8"}
        npm = node.parent / ("node_modules/npm/bin/npm-cli.js" if os.name == "nt" else "../lib/node_modules/npm/bin/npm-cli.js")
        if not npm.is_file():
            # Some managed runtimes ship node without npm. An existing exact
            # pnpm is sufficient and avoids acquiring another build tool.
            pnpm = shutil.which("pnpm")
            if not pnpm or subprocess.check_output([pnpm, "--version"], env=env).decode().strip() != PNPM_VERSION:
                raise RuntimeError("The pinned source-build runtime needs npm or pnpm " + PNPM_VERSION)
            command = [pnpm]
        else:
            command = [str(node), str(npm.resolve()), "exec", "--yes", "--package=pnpm@" + PNPM_VERSION, "--", "pnpm"]
        for args in (("install", "--frozen-lockfile", "--ignore-scripts"), ("typecheck",), ("build",)):
            subprocess.run([*command, *args], cwd=stage / "decky", env=env, check=True)
        plugin = output / "SOURCES" / f"bazzite-portal-decky-{provenance['version']}.zip"
        command = [sys.executable, str(stage / "decky/build.py"), "--output", str(plugin),
                   "--revision", provenance["revision"], "--source-sha256", provenance["source_sha256"],
                   "--dirty" if provenance["dirty"] else "--clean"]
        if wheel:
            command += ["--wheel", str(Path(wheel).resolve())]
        subprocess.run(command, cwd=stage, env=env, check=True)
        verify_zip(plugin, provenance["plugin_version"], provenance["revision"], provenance["source_sha256"], not worktree)
        files["packaging/source.json"] = (json.dumps(provenance, sort_keys=True).encode() + b"\n", 0o644)
        source = output / "SOURCES" / f"bazzite-portal-{provenance['version']}.tar.gz"
        write_tar(source, files, "bazzite-portal-" + provenance["version"], epoch)
        subprocess.run([sys.executable, str(stage / "packaging/metadata.py")], cwd=stage, env=env, check=True)
        shutil.copyfile(stage / "build/packaging/bazzite-portal.spec", output / "SPECS/bazzite-portal.spec")
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="build/rpmbuild")
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--worktree", action="store_true")
    parser.add_argument("--wheel")
    parser.add_argument("--verify-zip", type=Path)
    parser.add_argument("--expected-version", help="Expected Decky plugin version, independent of the RPM version")
    parser.add_argument("--expected-revision")
    parser.add_argument("--source-info", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    if args.verify_zip:
        expected = json.loads(args.source_info.read_text()) if args.source_info else {}
        version = args.expected_version or expected.get("plugin_version")
        if not version:
            parser.error("ZIP verification requires --expected-version or --source-info")
        verify_zip(args.verify_zip, version, args.expected_revision or expected.get("revision"),
                   expected.get("source_sha256"), args.require_clean)
    else:
        print(json.dumps(prepare(args.output, revision=args.revision, worktree=args.worktree, wheel=args.wheel), sort_keys=True))


if __name__ == "__main__":
    main()
