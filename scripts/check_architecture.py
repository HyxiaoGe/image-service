#!/usr/bin/env python3
"""Check that src/ layer dependency rules are respected.

Layer rules (higher layers may import from lower, never the reverse):
  api, mcp  ->  middleware, services, models, config
  middleware ->  models, config
  services   ->  models, config
  models     ->  (leaf, no src imports)
  config     ->  (leaf, no src imports)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Map each layer to the set of src layers it is ALLOWED to import from.
ALLOWED_IMPORTS: dict[str, set[str]] = {
    "api": {"middleware", "services", "models", "config", "mcp"},
    "mcp": {"services", "models", "config"},
    "middleware": {"models", "config"},
    "services": {"models", "config"},
    "models": set(),
    "config": set(),
}

SRC_DIR = Path(__file__).resolve().parent.parent / "src"


def get_layer(filepath: Path) -> str | None:
    """Determine which layer a file belongs to."""
    rel = filepath.relative_to(SRC_DIR)
    parts = rel.parts
    if len(parts) == 1:
        # Top-level file like config.py, main.py
        stem = parts[0].removesuffix(".py")
        if stem in ALLOWED_IMPORTS:
            return stem
        return None  # main.py is the composition root, skip
    return parts[0] if parts[0] in ALLOWED_IMPORTS else None


def extract_src_imports(filepath: Path) -> list[tuple[int, str]]:
    """Return list of (line_number, imported_module) for src.* imports."""
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src."):
            results.append((node.lineno, node.module))
    return results


def imported_layer(module: str) -> str | None:
    """Extract the layer name from a module path like 'src.services.cache'."""
    parts = module.split(".")
    if len(parts) < 2:
        return None
    target = parts[1]
    return target if target in ALLOWED_IMPORTS else None


def main() -> int:
    violations: list[str] = []

    for pyfile in sorted(SRC_DIR.rglob("*.py")):
        if "__pycache__" in str(pyfile):
            continue
        layer = get_layer(pyfile)
        if layer is None:
            continue

        allowed = ALLOWED_IMPORTS[layer]
        for lineno, module in extract_src_imports(pyfile):
            target_layer = imported_layer(module)
            if target_layer is None:
                continue
            if target_layer == layer:
                continue  # Same-layer imports are fine
            if target_layer not in allowed:
                rel_path = pyfile.relative_to(SRC_DIR.parent)
                violations.append(f"  {rel_path}:{lineno}: {layer} -> {target_layer} (import {module})")

    if violations:
        print("Architecture violations found:")
        for v in violations:
            print(v)
        return 1

    print("No architecture violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
