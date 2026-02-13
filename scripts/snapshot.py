"""
ARIA snapshot + ref system.

Parses Playwright 1.58+'s text-based aria_snapshot() output and assigns
deterministic refs (@e1, @e2, ...) to interactive and named content elements.

The aria_snapshot() format is YAML-like:
  - role "name":
    - child_role "child_name"
  - role:
    - child
  - text: "some text"
  - /url: https://example.com
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from typing import Any

# ---------------------------------------------------------------------------
# Role classification
# ---------------------------------------------------------------------------

INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "listbox", "menuitem", "option", "searchbox", "slider",
    "spinbutton", "switch", "tab", "treeitem", "menuitemcheckbox",
    "menuitemradio",
})

CONTENT_ROLES = frozenset({
    "heading", "cell", "gridcell", "columnheader", "rowheader",
    "listitem", "article", "region", "main", "navigation",
    "complementary", "banner", "contentinfo", "form", "search",
    "feed", "figure", "img", "math", "note", "status", "timer",
    "alert", "log", "marquee", "progressbar", "meter",
})

STRUCTURAL_ROLES = frozenset({
    "generic", "group", "list", "table", "row", "rowgroup",
    "menu", "toolbar", "tablist", "tabpanel", "tree", "treegrid",
    "grid", "presentation", "none", "separator", "dialog",
    "alertdialog", "application", "document", "directory",
    "paragraph",
})

# Metadata lines to skip
_SKIP_PREFIXES = ("- /url:", "- /src:", "- /alt:")

# Pattern: "- role "name" [attr=val]:" or "- role:" or "- role "name":"
# Also handles: "- text: content" and "- /url: ..."
# Name group handles escaped quotes: "Click \"here\"" etc.
_LINE_PATTERN = re.compile(
    r'^(\s*)-\s+'                          # indent + bullet
    r'(\w+)'                               # role
    r'(?:\s+"((?:[^"\\]|\\.)*)")?'         # optional "name" (handles escaped quotes)
    r'((?:\s+\[[\w]+=\w+\])*)'            # optional [attr=val] groups
    r'\s*:?\s*$'                           # optional trailing colon
)

_ATTR_PATTERN = re.compile(r'\[(\w+)=(\w+)\]')

# ---------------------------------------------------------------------------
# Ref counter (per-call, not global — avoids race across concurrent snapshots)
# ---------------------------------------------------------------------------


def _make_ref_counter() -> list[int]:
    """Create a local mutable ref counter (list-of-one-int trick)."""
    return [0]


def _next_ref(counter: list[int]) -> str:
    """Increment counter and return next ref like 'e1', 'e2', ..."""
    counter[0] += 1
    return f"e{counter[0]}"


# ---------------------------------------------------------------------------
# Duplicate tracker
# ---------------------------------------------------------------------------

class _RoleNameTracker:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._refs_by_key: dict[str, list[str]] = defaultdict(list)

    @staticmethod
    def key(role: str, name: str | None) -> str:
        return f"{role}:{name or ''}"

    def next_index(self, role: str, name: str | None) -> int:
        k = self.key(role, name)
        idx = self._counts[k]
        self._counts[k] += 1
        return idx

    def track(self, role: str, name: str | None, ref: str) -> None:
        self._refs_by_key[self.key(role, name)].append(ref)

    def duplicate_keys(self) -> set[str]:
        return {k for k, refs in self._refs_by_key.items() if len(refs) > 1}


def _build_selector(role: str, name: str | None) -> str:
    if name:
        escaped = name.replace('"', '\\"')
        return f'getByRole("{role}", name="{escaped}", exact=True)'
    return f'getByRole("{role}")'


# ---------------------------------------------------------------------------
# Line-level indentation
# ---------------------------------------------------------------------------

def _indent_level(line: str) -> int:
    """Count leading spaces (each 2 spaces = 1 level)."""
    stripped = line.lstrip(" ")
    spaces = len(line) - len(stripped)
    return spaces // 2


# ---------------------------------------------------------------------------
# Compact mode: check if subtree has interactive elements
# ---------------------------------------------------------------------------

def _subtree_has_interactive(lines: list[str], start: int, parent_indent: int) -> bool:
    """Check if any line in the subtree rooted at start has an interactive role."""
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        indent = _indent_level(line)
        if indent <= parent_indent:
            break  # exited subtree
        m = _LINE_PATTERN.match(line)
        if m:
            role = m.group(2).lower()
            name = m.group(3)
            if role in INTERACTIVE_ROLES:
                return True
            if role in CONTENT_ROLES and name:
                return True
    return False


# ---------------------------------------------------------------------------
# Core: process aria_snapshot text into ref-annotated tree
# ---------------------------------------------------------------------------

def process_aria_text(
    raw: str,
    compact: bool = True,
    max_depth: int = 10,
    counter: list[int] | None = None,
) -> tuple[str, dict[str, dict], list[int]]:
    """Parse Playwright aria_snapshot() text output.

    Returns (annotated_tree_text, ref_map, counter).
    The counter parameter allows callers to share ref numbering across
    multiple calls (e.g. cursor-interactive refs continue after ARIA refs).
    """
    if counter is None:
        counter = _make_ref_counter()
    refs: dict[str, dict] = {}
    tracker = _RoleNameTracker()
    out_lines: list[str] = []
    raw_lines = raw.splitlines()

    for i, line in enumerate(raw_lines):
        if not line.strip():
            continue

        # Skip metadata lines (/url, /src, etc.)
        stripped = line.strip()
        if any(stripped.startswith(p.strip()) for p in _SKIP_PREFIXES):
            continue

        # Handle plain text content lines
        if stripped.startswith("- text:"):
            text_content = stripped[len("- text:"):].strip().strip('"')
            if text_content and not compact:
                indent = "  " * _indent_level(line)
                out_lines.append(f'{indent}- text "{text_content}"')
            continue

        indent = _indent_level(line)
        if indent > max_depth:
            continue

        m = _LINE_PATTERN.match(line)
        if not m:
            # Non-matching line (inline text content, etc.)
            if not compact and stripped.startswith("- "):
                out_lines.append(line)
            continue

        indent_str = m.group(1)
        role = m.group(2).lower()
        name = m.group(3)  # may be None
        attrs_str = m.group(4) or ""

        # Parse attributes
        attrs = dict(_ATTR_PATTERN.findall(attrs_str))

        is_interactive = role in INTERACTIVE_ROLES
        is_content = role in CONTENT_ROLES
        is_structural = role in STRUCTURAL_ROLES

        # Compact: skip nameless structural roles with no interactive children
        if compact and is_structural and not name:
            if not _subtree_has_interactive(raw_lines, i, indent):
                continue
            # Has interactive children — skip this node but children will be processed
            continue

        should_ref = is_interactive or (is_content and name)

        # Build output line
        parts = [f"{'  ' * indent}- {role}"]

        if should_ref:
            ref = _next_ref(counter)
            nth = tracker.next_index(role, name)
            tracker.track(role, name, ref)
            refs[f"@{ref}"] = {
                "role": role,
                "name": name,
                "selector": _build_selector(role, name),
                "nth": nth,
            }
            if name:
                parts.append(f'"{name}"')
            parts.append(f"@{ref}")
        else:
            if name:
                parts.append(f'"{name}"')

        for attr_name, attr_val in attrs.items():
            parts.append(f"[{attr_name}={attr_val}]")

        out_lines.append(" ".join(parts))

    # Clean non-duplicate nth values
    dup_keys = tracker.duplicate_keys()
    for ref_data in refs.values():
        k = tracker.key(ref_data["role"], ref_data.get("name"))
        if k not in dup_keys:
            ref_data.pop("nth", None)

    return "\n".join(out_lines), refs, counter


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def take_snapshot(
    page,
    compact: bool = True,
    max_depth: int = 10,
    cursor_interactive: bool = True,
) -> dict:
    """Take an ARIA snapshot of the current page.

    Returns dict with: success, tree, refs, url, title, tab_count
    """
    try:
        raw = await page.locator(":root").aria_snapshot()
    except Exception as e:
        return {
            "success": False,
            "tree": "",
            "refs": {},
            "url": page.url,
            "title": await page.title(),
            "tab_count": len(page.context.pages),
            "error": f"ARIA snapshot failed: {e}",
        }

    if not raw:
        return {
            "success": False,
            "tree": "",
            "refs": {},
            "url": page.url,
            "title": await page.title(),
            "tab_count": len(page.context.pages),
            "error": "Empty ARIA snapshot — page may still be loading.",
        }

    counter = _make_ref_counter()
    tree_text, refs, counter = process_aria_text(
        raw, compact=compact, max_depth=max_depth, counter=counter,
    )

    # Cursor-interactive detection (continues ref numbering from ARIA refs)
    if cursor_interactive:
        cursor_els = await _find_cursor_interactive(page)
        existing_names = {
            (d.get("name") or "").lower()
            for d in refs.values()
            if d.get("name")
        }
        for el in cursor_els:
            if el["text"].lower() in existing_names:
                continue
            ref = _next_ref(counter)
            role = "clickable" if el.get("cursor_pointer") else "focusable"
            refs[f"@{ref}"] = {
                "role": role,
                "name": el["text"],
                "selector": el["selector"],
            }
            tree_text += f'\n- [cursor-interactive] "{el["text"]}" @{ref}'

    # Build header
    url = page.url
    title = await page.title()
    tab_count = len(page.context.pages)
    header = f"Page: {url} | Title: {title}\nTab {_find_tab_index(page)} of {tab_count}\n\n"

    return {
        "success": True,
        "tree": header + tree_text,
        "refs": refs,
        "url": url,
        "title": title,
        "tab_count": tab_count,
    }


def _find_tab_index(page) -> int:
    pages = page.context.pages
    for i, p in enumerate(pages):
        if p is page:
            return i + 1
    return 1


async def _find_cursor_interactive(page) -> list[dict]:
    """Find elements that are clickable but lack ARIA roles."""
    js = """
    () => {
        const interactiveTags = new Set([
            'a', 'button', 'input', 'select', 'textarea', 'summary', 'details'
        ]);
        const interactiveRoles = new Set([
            'button', 'link', 'textbox', 'checkbox', 'radio', 'combobox',
            'listbox', 'menuitem', 'option', 'searchbox', 'slider',
            'spinbutton', 'switch', 'tab', 'treeitem'
        ]);
        const results = [];
        const seen = new Set();

        for (const el of document.querySelectorAll('*')) {
            const tag = el.tagName.toLowerCase();
            if (interactiveTags.has(tag)) continue;

            const role = el.getAttribute('role');
            if (role && interactiveRoles.has(role)) continue;

            const style = getComputedStyle(el);
            const cursorPointer = style.cursor === 'pointer';
            const hasOnClick = el.hasAttribute('onclick') || el.onclick !== null;
            const tabIndex = el.getAttribute('tabindex');
            const hasTabIndex = tabIndex !== null && tabIndex !== '-1';

            if (!cursorPointer && !hasOnClick && !hasTabIndex) continue;

            const text = (el.textContent || '').trim().slice(0, 80);
            if (!text || seen.has(text)) continue;

            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;

            seen.add(text);

            let selector = tag;
            if (el.id) {
                selector = '#' + CSS.escape(el.id);
            } else if (el.className && typeof el.className === 'string') {
                const cls = el.className.trim().split(/\\s+/).slice(0, 2).map(c => '.' + CSS.escape(c)).join('');
                selector = tag + cls;
            }

            results.push({
                text: text,
                selector: selector,
                tag: tag,
                cursor_pointer: cursorPointer,
                has_onclick: hasOnClick,
                has_tabindex: hasTabIndex,
            });

            if (results.length >= 20) break;
        }
        return results;
    }
    """
    try:
        return await page.evaluate(js)
    except Exception:
        return []
