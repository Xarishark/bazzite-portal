"""Bounded, dependency-free Markdown rendering for GitHub release notes."""

from __future__ import annotations

from html.parser import HTMLParser
import html
import re
from typing import Any
from urllib.parse import urlsplit

MAX_BODY_BYTES = 64 * 1024


class _TextOnlyHTML(HTMLParser):
    """Drop raw HTML structure while retaining its human-readable text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "div", "details", "summary", "li", "br", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "details", "summary", "li", "br", "tr"}:
            self.parts.append(" ")


def _plain_html(value: str) -> str:
    parser = _TextOnlyHTML()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]*>", "", value)
    return "".join(parser.parts)


def _safe_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


_INLINE = re.compile(
    r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)|`([^`]+)`|\*\*([^*]+)\*\*|__([^_]+)__|~~([^~]+)~~|\*([^*]+)\*|_([^_]+)_"
)


def _inline_markup(value: str) -> str:
    """Convert the small inline subset used in release notes to Pango markup."""
    value = _plain_html(value)
    out: list[str] = []
    pos = 0
    for match in _INLINE.finditer(value):
        out.append(html.escape(value[pos:match.start()]))
        groups = match.groups()
        if groups[0] is not None:
            label, url = groups[0], groups[1]
            escaped = html.escape(label)
            if match.group(0).startswith("!"):
                out.append(escaped)
            elif _safe_url(url):
                out.append(f'<a href="{html.escape(url, quote=True)}">{escaped}</a>')
            else:
                out.append(escaped)
        elif groups[2] is not None:
            out.append(f'<tt>{html.escape(groups[2])}</tt>')
        elif groups[3] is not None or groups[4] is not None:
            out.append(f'<b>{html.escape(groups[3] or groups[4])}</b>')
        elif groups[5] is not None:
            out.append(f'<s>{html.escape(groups[5])}</s>')
        else:
            out.append(f'<i>{html.escape(groups[6] or groups[7])}</i>')
        pos = match.end()
    out.append(html.escape(value[pos:]))
    return "".join(out)


def parse_release_notes(body: str) -> list[dict[str, Any]]:
    """Parse common GitHub release Markdown into safe, renderer-neutral blocks.

    Input is capped to the same 64 KiB byte bound as the release lookup. Raw
    HTML is reduced to visible text; it is never interpreted as UI markup.
    """
    if not isinstance(body, str):
        return []
    body = body.encode("utf-8")[:MAX_BODY_BYTES].decode("utf-8", errors="ignore")
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append({"type": "paragraph", "text": _plain_html(" ".join(s.strip() for s in paragraph))})
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush(); i += 1; continue
        fence = re.match(r"^\s*(```+|~~~+)(.*)$", line)
        if fence:
            flush(); marker = fence.group(1); code: list[str] = []; i += 1
            while i < len(lines) and not re.match(r"^\s*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$", lines[i]):
                code.append(lines[i]); i += 1
            blocks.append({"type": "code", "text": "\n".join(code)})
            i += 1; continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush(); blocks.append({"type": "heading", "level": len(heading.group(1)), "text": _plain_html(heading.group(2))}); i += 1; continue
        if i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", lines[i + 1]):
            flush()
            def cells(source: str) -> list[str]:
                source = source.strip().strip("|")
                result: list[str] = []; part = ""; escaped = False
                for char in source:
                    if char == "|" and not escaped:
                        result.append(part.strip()); part = ""
                    else:
                        part += char
                    escaped = char == "\\" and not escaped
                    if char != "\\": escaped = False
                result.append(part.strip())
                return [_plain_html(cell) for cell in result]
            headers = cells(line)
            i += 2; rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                row = cells(lines[i]); rows.append((row + [""] * len(headers))[:len(headers)]); i += 1
            blocks.append({"type": "table", "headers": headers, "rows": rows}); continue
        list_match = re.match(r"^(\s*)([-+*]|\d+[.)])\s+(.*)$", line)
        if list_match:
            flush(); ordered = list_match.group(2)[0].isdigit(); items = []
            while i < len(lines):
                item = re.match(r"^(\s*)([-+*]|\d+[.)])\s+(.*)$", lines[i])
                if not item or item.group(2)[0].isdigit() != ordered: break
                items.append(_plain_html(item.group(3))); i += 1
            blocks.append({"type": "list", "ordered": ordered, "items": items}); continue
        quote = re.match(r"^\s*>\s?(.*)$", line)
        if quote:
            flush(); quote_lines = []
            while i < len(lines):
                q = re.match(r"^\s*>\s?(.*)$", lines[i])
                if not q: break
                quote_lines.append(q.group(1)); i += 1
            blocks.append({"type": "quote", "text": _plain_html(" ".join(quote_lines))}); continue
        # HTML details/summary and other raw tags are rendered as their text.
        paragraph.append(line); i += 1
    flush()
    return blocks


def build_release_notes(body: str):
    """Build a native GTK box from release Markdown; GTK is imported lazily."""
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, Pango

    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    root.add_css_class("release-notes")

    def label(text: str, style: str | None = None, wrap: bool = True):
        widget = Gtk.Label()
        widget.set_markup(text)
        widget.set_xalign(0)
        widget.set_wrap(wrap)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        widget.set_hexpand(True)
        widget.set_max_width_chars(70)
        widget.set_selectable(True)
        if style:
            widget.add_css_class(style)
        return widget

    for block in parse_release_notes(body):
        kind = block["type"]
        if kind == "heading":
            level = block["level"]
            title = _inline_markup(block["text"])
            root.append(label(f"<span size='{('x-large' if level <= 2 else 'large' if level <= 4 else 'medium')}' weight='bold'>{title}</span>", "heading"))
        elif kind == "paragraph":
            root.append(label(_inline_markup(block["text"]), "paragraph"))
        elif kind == "code":
            root.append(label(f"<tt>{html.escape(block['text'])}</tt>", "code-block"))
        elif kind == "quote":
            quote = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            quote.add_css_class("quote")
            quote.append(label("▏", "quote-mark", False)); quote.append(label(_inline_markup(block["text"])))
            root.append(quote)
        elif kind == "list":
            for index, item in enumerate(block["items"], 1):
                bullet = f"{index}." if block["ordered"] else "•"
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.append(label(html.escape(bullet), "list-marker", False)); row.append(label(_inline_markup(item)))
                root.append(row)
        elif kind == "table":
            headers, rows = block["headers"], block["rows"]
            grid = Gtk.Grid(column_spacing=0, row_spacing=0)
            grid.add_css_class("release-table")
            for column, value in enumerate(headers):
                cell = label(f"<b>{_inline_markup(value)}</b>", "table-header")
                grid.attach(cell, column, 0, 1, 1)
            for row_index, values in enumerate(rows, 1):
                for column, value in enumerate(values):
                    cell = label(_inline_markup(value), "table-cell")
                    if row_index % 2 == 0:
                        cell.add_css_class("alternate-row")
                    grid.attach(cell, column, row_index, 1, 1)
            root.append(grid)
    return root
