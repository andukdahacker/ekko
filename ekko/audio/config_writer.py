"""Portable TOML writer for capture.input_device — shared by all backends."""
from __future__ import annotations

from pathlib import Path


def set_capture_input_device(config_path: Path | None, device_name: str) -> Path:
    """Set capture.input_device in the TOML config, preserving the rest of the
    file. Handles: existing (possibly commented) input_device line, an existing
    [capture] table with no such line, or no [capture] table at all.
    """
    from ..config import DEFAULT_CONFIG
    path = config_path or DEFAULT_CONFIG
    new_line = f'input_device = "{device_name}"'

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[capture]\n{new_line}\n", encoding="utf-8")
        return path

    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_capture = False
    replaced = False
    capture_header_idx = -1

    def is_table_header(s: str) -> bool:
        t = s.strip()
        return t.startswith("[") and t.endswith("]")

    for line in lines:
        stripped = line.strip()
        if is_table_header(line):
            if in_capture and not replaced:       # leaving [capture] unreplaced
                out.append(new_line)
                replaced = True
            in_capture = (stripped == "[capture]")
            if in_capture:
                capture_header_idx = len(out)
            out.append(line)
            continue
        if in_capture and not replaced and stripped.lstrip("#").strip().startswith("input_device"):
            out.append(new_line)
            replaced = True
            continue
        out.append(line)

    if in_capture and not replaced:               # file ended inside [capture]
        out.append(new_line)
        replaced = True
    if not replaced and capture_header_idx == -1:  # no [capture] table at all
        out += ["", "[capture]", new_line]

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path
