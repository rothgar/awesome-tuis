#!/usr/bin/env python3
"""
Alphabetical-order check for README.md additions.

For each section (h2 within <details>) and sub-section (h3 inside an
h2 section), verify that list items are sorted alphabetically (case-
insensitive). Only flag violations that involve a line added by the PR,
not pre-existing misordering.
"""
import re


ITEM_RE = re.compile(r"^- \[([^\]]+)\]")
H2_OPEN_RE = re.compile(r"<details\b[^>]*>\s*<summary>\s*<h2>([^<]+)</h2>\s*</summary>", re.IGNORECASE)
H3_RE = re.compile(r"<h3>([^<]+)</h3>", re.IGNORECASE)
DETAILS_CLOSE_RE = re.compile(r"</details>", re.IGNORECASE)
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_sections(content: str) -> list[dict]:
    """Parse README into sections.

    Returns a list of:
        {"name": str, "items": [(line_no, name)], "subsections": [{name, items}]}

    Sections with sub-sections (e.g. Libraries) yield one entry per
    sub-section in `subsections` and the top-level `items` is empty.
    """
    sections: list[dict] = []
    current: dict | None = None
    current_sub: dict | None = None

    for i, raw in enumerate(content.splitlines(), start=1):
        line = raw.rstrip("\r")

        if H2_OPEN_RE.search(line):
            current = {"name": H2_OPEN_RE.search(line).group(1).strip(), "items": [], "subsections": []}
            current_sub = None
            sections.append(current)
            continue

        if H3_RE.search(line):
            if current is not None:
                current_sub = {"name": H3_RE.search(line).group(1).strip(), "items": []}
                current["subsections"].append(current_sub)
            continue

        if DETAILS_CLOSE_RE.search(line):
            current = None
            current_sub = None
            continue

        m = ITEM_RE.match(line.strip())
        if not m:
            continue
        name = m.group(1).strip()
        entry = (i, name)
        if current_sub is not None:
            current_sub["items"].append(entry)
        elif current is not None:
            current["items"].append(entry)
    return sections


def iter_checkable_groups(sections: list[dict]) -> list[tuple[str, list[tuple[int, str]]]]:
    """Return (display_name, items) pairs we should check for ordering."""
    out: list[tuple[str, list[tuple[int, str]]]] = []
    for s in sections:
        if s["subsections"]:
            for sub in s["subsections"]:
                out.append((f"{s['name']} > {sub['name']}", sub["items"]))
        else:
            out.append((s["name"], s["items"]))
    return out


def apply_patch(base_lines: list[str], patch_text: str) -> tuple[list[str], set[int]]:
    """Reconstruct the new file from base content and a unified diff.

    Returns (new_lines_with_trailing_newlines, set_of_added_line_numbers_1indexed).
    """
    new_lines: list[str] = []
    added: set[int] = set()

    hunks: list[dict] = []
    current: dict | None = None
    for line in patch_text.splitlines(keepends=False):
        m = HUNK_RE.match(line)
        if m:
            if current is not None:
                hunks.append(current)
            current = {
                "old_start": int(m.group(1)),
                "new_start": int(m.group(2)),
                "lines": [],
            }
        elif current is not None:
            current["lines"].append(line)
    if current is not None:
        hunks.append(current)

    old_pos = 1
    new_pos = 1
    for hunk in hunks:
        while old_pos < hunk["old_start"]:
            new_lines.append(base_lines[old_pos - 1])
            old_pos += 1
            new_pos += 1
        for hunk_line in hunk["lines"]:
            if hunk_line.startswith("---") or hunk_line.startswith("+++"):
                continue
            if hunk_line.startswith("\\"):
                # "\ No newline at end of file" marker — skip
                continue
            if hunk_line.startswith("-"):
                old_pos += 1
            elif hunk_line.startswith("+"):
                content = hunk_line[1:]
                if not content.endswith("\n"):
                    content += "\n"
                new_lines.append(content)
                added.add(new_pos)
                new_pos += 1
            elif hunk_line.startswith(" "):
                new_lines.append(base_lines[old_pos - 1])
                old_pos += 1
                new_pos += 1
    while old_pos <= len(base_lines):
        new_lines.append(base_lines[old_pos - 1])
        old_pos += 1
        new_pos += 1
    return new_lines, added


def collect_added_lines_for_file(changed_file: dict) -> set[int]:
    """Parse a single changed-file's patch and return added line numbers (1-indexed)."""
    patch = (changed_file or {}).get("patch") or ""
    if not patch:
        return set()
    added: set[int] = set()
    current_new: int | None = None
    for line in patch.splitlines():
        m = HUNK_RE.match(line)
        if m:
            current_new = int(m.group(2))
            continue
        if current_new is None:
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.add(current_new)
            current_new += 1
        elif line.startswith("-"):
            continue
        elif line.startswith("\\"):
            continue
        else:
            current_new += 1
    return added


def find_ordering_violations(
    items: list[tuple[int, str]],
    added_lines: set[int],
) -> tuple[bool, list[str]]:
    """Check if `items` is alphabetically sorted (case-insensitive).

    Returns (involves_added_line, list_of_descriptions).
    Only violations where at least one of the two items is on a line
    added by the PR are reported (and `involves_added_line` is True iff
    any such violation exists). Pre-existing misordering is ignored.
    """
    names = [n.casefold() for _, n in items]
    descriptions: list[str] = []
    involves_added = False
    for i in range(len(items) - 1):
        if names[i] > names[i + 1]:
            line_a, name_a = items[i]
            line_b, name_b = items[i + 1]
            pair_involves_added = (line_a in added_lines) or (line_b in added_lines)
            if pair_involves_added:
                involves_added = True
                descriptions.append(
                    f"`{name_a}` (line {line_a}) should come after `{name_b}` (line {line_b})"
                )
    return involves_added, descriptions


def check_alphabetical(base_content: str, readme_patch: str) -> str:
    """Return a markdown section (empty string if everything is sorted).

    `base_content` is the README on the base branch.
    `readme_patch` is the unified diff for README.md from the PR.
    """
    base_lines = base_content.splitlines(keepends=True)
    new_lines, added_lines = apply_patch(base_lines, readme_patch)
    new_content = "".join(new_lines)

    sections = parse_sections(new_content)
    flagged: list[tuple[str, list[str]]] = []
    for group_name, items in iter_checkable_groups(sections):
        if len(items) < 2:
            continue
        involves_added, descriptions = find_ordering_violations(items, added_lines)
        if involves_added and descriptions:
            flagged.append((group_name, descriptions))

    if not flagged:
        return ""

    lines = [
        "",
        "### Alphabetical order",
        "",
        "Items below are not in alphabetical order within their section. "
        "Please sort the affected entries so the section stays alphabetical.",
        "",
    ]
    for group_name, descriptions in flagged:
        lines.append(f"- **{group_name}**")
        for d in descriptions:
            lines.append(f"  - {d}")
    return "\n".join(lines) + "\n"
