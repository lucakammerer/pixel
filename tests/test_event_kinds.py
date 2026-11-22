"""Every literal event kind emitted in pixel/ is one the log knows: #336 found booking.cancelled raising live."""

import ast
from pathlib import Path

from pixel.core.events import KINDS

ROOT = Path(__file__).resolve().parent.parent / "pixel"


def test_every_emitted_kind_is_known():
    unknown = []
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name == "emit" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                kind = node.args[1].value
                if isinstance(kind, str) and kind not in KINDS:
                    unknown.append(f"{path.relative_to(ROOT)}:{node.lineno} {kind}")
    assert unknown == []
