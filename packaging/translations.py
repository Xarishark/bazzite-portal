"""Generate runtime message resources and compile translations for RPMs."""
import argparse
import ast
import json
from pathlib import Path
import re
import shutil
from string import Formatter
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "bazzite-portal"
APP_ID = "io.github.Xarishark.bazzite_portal"
DESKTOPS = (f"{APP_ID}.desktop", "bazzite-portal-agent.desktop")
METAINFO = f"{APP_ID}.metainfo.xml"


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def message_templates(sources):
    templates = set()
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id in {"_", "gettext", "N_", "C_", "ngettext", "pgettext"}:
                if node.args and isinstance(node.args[0], ast.JoinedStr):
                    raise ValueError(f"{path}:{node.lineno}: translate the literal before formatting")
                # Layout-only templates create work without any words to translate.
                for argument in node.args:
                    if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                        continue
                    value = argument.value
                    literal = re.sub(r"\{[^{}]+\}", "", value)
                    if value and not any(char.isalpha() for char in literal):
                        raise ValueError(f"{path}:{node.lineno}: keep formatting-only text outside gettext")
            if node.func.id != "C_":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                raise ValueError(f"{path}:{node.lineno}: C_ requires a literal template")
            template = node.args[0].value
            for _, name, spec, conversion in Formatter().parse(template):
                if name is not None and (not name.isidentifier() or spec or conversion):
                    raise ValueError(f"{path}:{node.lineno}: use simple named C_ placeholders")
            templates.add(template)
    # Specific templates first so a broad error wrapper cannot swallow one.
    return sorted(templates, key=lambda text: (-len(re.sub(r"\{[^}]+\}", "", text)), text))


def catalogs():
    for path in sorted((ROOT / "po").glob("*.po")):
        if not re.fullmatch(r"[a-z]{2,3}(?:_[A-Za-z0-9]+)*(?:@[a-zA-Z0-9]+)?", path.stem):
            raise ValueError(f"Invalid gettext language filename: {path.name}")
        yield path


def user_messages():
    """Literal presentation messages that may safely cross an error boundary."""
    result = set()
    for path in (ROOT / "bazzite_portal").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"_", "N_", "gettext"} and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                value = node.args[0].value
                if value and "{" not in value:
                    result.add(value)
    return json.dumps(sorted(result), ensure_ascii=False, indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "build"))
    args = parser.parse_args()
    sources = sorted((ROOT / "bazzite_portal").rglob("*.py"))
    templates = message_templates(sources)
    for path in catalogs():
        run("msgfmt", "--check", "--check-format", "-o", "/dev/null", str(path))
    if args.command == "build":
        # Weblate owns POT/PO updates. These runtime resources are derived only
        # from source and generated before the wheel is assembled.
        core = ROOT / "bazzite_portal/core"
        (core / "message_templates.json").write_text(
            json.dumps(templates, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        (core / "user_messages.json").write_text(
            user_messages(), encoding="utf-8", newline="\n")
        run(sys.executable, "packaging/metadata.py")
        output = ROOT / "build"
        for path in catalogs():
            target = output / "locale" / path.stem / "LC_MESSAGES" / f"{DOMAIN}.mo"
            target.parent.mkdir(parents=True, exist_ok=True)
            run("msgfmt", "--check", "--check-format", "-o", str(target), str(path))
        metadata = output / "packaging"
        metadata.mkdir(parents=True, exist_ok=True)
        # gettext's merge modes use LINGUAS; generate it from the checked files
        # in a temporary directory so Weblate only has to maintain .po files.
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "LINGUAS").write_text("\n".join(p.stem for p in catalogs()) + "\n")
            for path in catalogs():
                shutil.copyfile(path, directory / path.name)
            for name in DESKTOPS:
                run("msgfmt", "--desktop", "--template", str(ROOT / "packaging" / name),
                    "-d", str(directory), "-o", str(metadata / name))
            run("msgfmt", "--xml", "--template", str(metadata / (METAINFO + ".in")),
                "-d", str(directory), "-o", str(metadata / METAINFO))
        # The RPM installs this directory even before the first translation.
        (output / "locale").mkdir(exist_ok=True)


if __name__ == "__main__":
    main()
