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
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "bazzite-portal"
APP_ID = "io.github.Xarishark.bazzite_portal"
DESKTOPS = (f"{APP_ID}.desktop", "bazzite-portal-agent.desktop")
METAINFO = f"{APP_ID}.metainfo.xml"
METADATA_PLACEHOLDERS = {
    "{bazzite}": "Bazzite",
    "{uupd}": "UUPD",
}
DESKTOP_NON_TEXT_KEYS = {
    "Actions", "Categories", "DBusActivatable", "Exec", "Icon", "MimeType",
    "NoDisplay", "NotShowIn", "OnlyShowIn", "Path", "StartupWMClass", "Terminal",
    "TryExec", "Type", "X-GNOME-Autostart-enabled",
}


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
            if not isinstance(node, ast.Call):
                continue

            # Include literal gettext templates formatted with literal values.
            # A runtime value or unresolved placeholder is not safe to add.
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "format"
                    and isinstance(node.func.value, ast.Call)):
                translated = node.func.value
                if (not isinstance(translated.func, ast.Name)
                        or translated.func.id not in {"_", "N_", "gettext"}
                        or not translated.args
                        or not isinstance(translated.args[0], ast.Constant)
                        or not isinstance(translated.args[0].value, str)
                        or translated.keywords
                        or node.args):
                    continue
                if any(keyword.arg is None or not isinstance(keyword.value, ast.Constant)
                       or not isinstance(keyword.value.value, str)
                       for keyword in node.keywords):
                    continue
                try:
                    value = translated.args[0].value.format(
                        **{keyword.arg: keyword.value.value for keyword in node.keywords})
                except (KeyError, IndexError, ValueError):
                    continue
            elif (isinstance(node.func, ast.Name)
                    and node.func.id in {"_", "N_", "gettext", "C_"}
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                value = node.args[0].value
                if node.func.id == "C_":
                    # Only static C_ calls are safe to include in this allowlist.
                    # Runtime values and **kwargs must never be converted into a
                    # message that appears safe to show across an error boundary.
                    if any(keyword.arg is None or not isinstance(keyword.value, ast.Constant)
                           or not isinstance(keyword.value.value, str)
                           for keyword in node.keywords):
                        continue
                    try:
                        value = value.format(**{
                            keyword.arg: keyword.value.value for keyword in node.keywords
                        })
                    except (KeyError, IndexError, ValueError):
                        continue
            else:
                continue
            if value and "{" not in value and "}" not in value:
                result.add(value)
    return json.dumps(sorted(result), ensure_ascii=False, indent=2) + "\n"


def fix_desktop_product_name(path, product_name, prefix_descriptor=False):
    """Keep launcher names fixed and prefix translated monitor descriptors."""
    lines = []
    in_desktop_entry = False
    has_base_name = False
    for line in path.read_text(encoding="utf-8").splitlines():
        section = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if section:
            in_desktop_entry = section.group(1) == "Desktop Entry"
            if in_desktop_entry:
                lines.append(line)
                continue
        if in_desktop_entry:
            name = re.match(r"^(\s*)Name(\[[^]]+\])?=(.*)$", line)
            if name:
                if name.group(2):
                    if prefix_descriptor:
                        line = f"{name.group(1)}Name{name.group(2)}={product_name} {name.group(3)}"
                    else:
                        continue
                else:
                    has_base_name = True
                    value = (f"{product_name} {name.group(3)}" if prefix_descriptor
                             else product_name)
                    line = f"{name.group(1)}Name={value}"
        lines.append(line)
    if not has_base_name:
        for index, line in enumerate(lines):
            if re.match(r"^\s*\[Desktop Entry\]\s*$", line):
                lines.insert(index + 1, f"Name={product_name}")
                break
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def replace_desktop_placeholders(path):
    """Expand fixed brand tokens in desktop display text for every locale."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(\s*)([^=]+)=(.*)$", line)
        if match:
            field = match.group(2)
            key = re.sub(r"\[[^]]+\]$", "", field)
            if key not in DESKTOP_NON_TEXT_KEYS:
                value = match.group(3)
                for token, replacement in METADATA_PLACEHOLDERS.items():
                    value = value.replace(token, replacement)
                line = f"{match.group(1)}{field}={value}"
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def keep_metainfo_name_fixed(path):
    """Remove the extraction hint and localized AppStream name elements."""
    tree = ET.parse(path)
    component = tree.getroot()
    lang_attribute = "{http://www.w3.org/XML/1998/namespace}lang"
    for name in component.findall("name"):
        name.attrib.pop("translate", None)
        if lang_attribute in name.attrib:
            component.remove(name)
    for element in component.iter():
        if element.tag.split("}")[-1] in {"id", "url", "launchable", "provides", "binary"}:
            continue
        if element.text:
            for token, replacement in METADATA_PLACEHOLDERS.items():
                element.text = element.text.replace(token, replacement)
        if element.tail:
            for token, replacement in METADATA_PLACEHOLDERS.items():
                element.tail = element.tail.replace(token, replacement)
    tree.write(path, encoding="UTF-8", xml_declaration=True)


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
                target = metadata / name
                run("msgfmt", "--desktop", "--template", str(ROOT / "packaging" / name),
                    "-d", str(directory), "-o", str(target))
                # gettext translates the complete Name field as one string.
                # The agent descriptor is a separate translatable msgid; prefix
                # its generated locale values with the fixed product name.
                if name == "bazzite-portal-agent.desktop":
                    fix_desktop_product_name(target, "Bazzite Portal", prefix_descriptor=True)
                else:
                    fix_desktop_product_name(target, "Bazzite Portal")
                replace_desktop_placeholders(target)
            run("msgfmt", "--xml", "--template", str(metadata / (METAINFO + ".in")),
                "-d", str(directory), "-o", str(metadata / METAINFO))
            # AppStream's <name> has the same whole-string limitation.
            keep_metainfo_name_fixed(metadata / METAINFO)
        # The RPM installs this directory even before the first translation.
        (output / "locale").mkdir(exist_ok=True)


if __name__ == "__main__":
    main()
