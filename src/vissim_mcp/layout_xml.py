"""View layout copy: read the `.layx` next to a network and write a new file with only four display attributes changed.

The original is never modified. Vissim COM cannot show background images or 3D models or turn off the online map
(checked on 2026 SP09), so only this file is edited as text. Only the `LAYX_VIEW` attribute values inside
`<networkEditorLayouts>` change and every other byte is kept. The written file is parsed again and deleted if it does
not match.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# (element, attribute) -> value. Show background images and 3D models; turn off the online map in 2D and 3D
# (it sends an external request every time the network opens).
LAYX_VIEW: dict[tuple[str, str], str] = {
    ("backgroundGPars", "objectVisibility"): "true",
    ("static3DModelGPars", "objectVisibility"): "true",
    ("baseGPars", "showMap"): "false",
    ("gPars3D", "showMap"): "false",
}
_LAYX_SPAN = re.compile(r"<networkEditorLayouts>.*?</networkEditorLayouts>", re.S)


class LayoutFormatError(ValueError):
    """The `.layx` does not have the expected shape (<networkEditorLayouts>, the four attributes in every layout)."""


def _patch_layout_display(text: str, values: dict[tuple[str, str], str], name: str) -> tuple[str, int, int]:
    """Return the text with only the (element, attribute) values replaced in every layout of `<networkEditorLayouts>`.
    Returns (new text, number of layouts, number of values changed). Raises LayoutFormatError unless every layout has
    exactly one of each element."""
    m = _LAYX_SPAN.search(text)
    if m is None:
        raise LayoutFormatError(f"{name} has no <networkEditorLayouts>")
    span = m.group(0)
    n = span.count("<networkEditorLayout ")
    changed = 0
    for (tag, attr), val in values.items():
        pat = re.compile(rf'(<{tag}\b[^>]*?\s{attr}=")(true|false)(")')
        found = pat.findall(span)
        if n == 0 or len(found) != n:
            raise LayoutFormatError(f"{name}: <{tag} {attr}> found in {len(found)} of {n} layouts")
        changed += sum(1 for f in found if f[1] != val)
        span = pat.sub(rf"\g<1>{val}\g<3>", span)
    return text[:m.start()] + span + text[m.end():], n, changed


def _layout_display_problems(path: Path, values: dict[tuple[str, str], str], n: int) -> list[str]:
    """Parse the written file again and check every layout has `values`. Returns the mismatches (empty means OK)."""
    bad: list[str] = []
    try:
        root = ET.parse(path).getroot()
        lays = root.findall("networkEditorLayouts/networkEditorLayout")
        if len(lays) != n:
            bad.append(f"layout count {len(lays)} != {n}")
        for lay in lays:
            for (tag, attr), val in values.items():
                vals = [e.get(attr) for e in lay.iter(tag)]
                if not vals or any(v != val for v in vals):
                    bad.append(f"layout {lay.get('no')} <{tag} {attr}>={vals}")
    except ET.ParseError as e:
        bad.append(f"not well-formed: {e}")
    return bad


def write_view_layout(src: Path, dst: Path) -> dict[str, Any]:
    """Write a view layout to the new file `dst`, based on `src` (the `.layx` next to the network, read only), with
    background images and 3D models shown and the online map off. The original is not touched. Only the four
    `LAYX_VIEW` values change and every other byte is kept. The result is parsed again; on a mismatch `dst` is
    deleted and LayoutFormatError is raised. Returns {layouts, changed, path}."""
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise FileNotFoundError(f"Layout file not found: {src}")
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
        raise LayoutFormatError(f"The view layout must be a different file from the original: {dst}")
    text = src.read_bytes().decode("utf-8")
    new_text, n, changed = _patch_layout_display(text, LAYX_VIEW, src.name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(new_text.encode("utf-8"))
    bad = _layout_display_problems(dst, LAYX_VIEW, n)
    if bad:
        dst.unlink(missing_ok=True)
        raise LayoutFormatError(f"{dst.name} failed re-validation and was deleted: {'; '.join(bad)}")
    return {"layouts": n, "changed": changed, "path": str(dst)}
