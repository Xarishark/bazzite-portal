"""Maintain gettext sources and compile the catalogs/desktop metadata for RPMs."""
import argparse
import ast
import json
from pathlib import Path
import re
import shutil
from string import Formatter
import subprocess
import tempfile
import tomllib

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


def extract(destination):
    sources = sorted((ROOT / "bazzite_portal").rglob("*.py"))
    run("xgettext", "--language=Python", "--from-code=UTF-8", "--no-wrap", "--sort-output",
        "--keyword=_", "--keyword=gettext", "--keyword=N_", "--keyword=C_",
        "--keyword=ngettext:1,2", "--keyword=pgettext:1c,2",

        "--flag=C_:1:python-brace-format", "--add-comments=Translators:",
        "--package-name=Bazzite Portal",
        "--package-version=" + tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
        "--msgid-bugs-address=https://github.com/Xarishark/bazzite-portal/issues",
        "--copyright-holder=Bazzite Portal contributors", "-o", str(destination),
        *(str(path.relative_to(ROOT)) for path in sources))
    run("xgettext", "--language=Desktop", "--from-code=UTF-8", "--no-wrap", "--sort-output",
        "--join-existing", "-o", str(destination),
        *(f"packaging/{name}" for name in DESKTOPS))
    its = Path("/usr/share/gettext/its/metainfo.its")
    if not its.is_file():
        raise RuntimeError("Install appstream: its gettext metadata extraction rules are required")
    run("xgettext", f"--its={its}", "--from-code=UTF-8", "--no-wrap", "--sort-output",
        "--join-existing", "-o", str(destination), f"packaging/{METAINFO}")
    text = destination.read_text(encoding="utf-8")
    # Omit extraction timestamps so check is reproducible across machines/runs.
    text = re.sub(r'^"POT-Creation-Date:.*\n', "", text, flags=re.MULTILINE)
    text = text.replace("charset=CHARSET", "charset=UTF-8")
    destination.write_text(text, encoding="utf-8", newline="\n")
    return message_templates(sources)


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
    parser.add_argument("command", choices=("update", "check", "build"))
    args = parser.parse_args()
    po = ROOT / "po"
    po.mkdir(exist_ok=True)
    template = po / f"{DOMAIN}.pot"
    patterns = ROOT / "bazzite_portal/core/message_templates.json"
    public_messages = ROOT / "bazzite_portal/core/user_messages.json"
    if args.command in {"update", "check"}:
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / template.name
            messages = extract(generated)
            pattern_text = json.dumps(messages, ensure_ascii=False, indent=2) + "\n"
            public_text = user_messages()
            if args.command == "update":
                shutil.copyfile(generated, template)
                patterns.write_text(pattern_text, encoding="utf-8", newline="\n")
                public_messages.write_text(public_text, encoding="utf-8", newline="\n")
            elif (not template.is_file() or generated.read_bytes() != template.read_bytes()
                  or not patterns.is_file() or patterns.read_text(encoding="utf-8") != pattern_text
                  or not public_messages.is_file() or public_messages.read_text(encoding="utf-8") != public_text):
                raise SystemExit("Translation template is stale: run python3 packaging/translations.py update")
    for path in catalogs():
        run("msgfmt", "--check", "--check-format", "-o", "/dev/null", str(path))
    if args.command == "build":
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
            run("msgfmt", "--xml", "--template", str(ROOT / "packaging" / METAINFO),
                "-d", str(directory), "-o", str(metadata / METAINFO))
        # The RPM installs this directory even before the first translation.
        (output / "locale").mkdir(exist_ok=True)


if __name__ == "__main__":
    main()
