"""Static checks on the simulator page's inline GLSL."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "static" / "lamp-simulator.html"


def shader_block(name: str) -> str:
    source = PAGE.read_text(encoding="utf-8")
    match = re.search(rf"\n      const {name} =\n(.*?;)\n", source, re.S)
    assert match, f"const {name} not found in {PAGE}"
    return match.group(1)


class TestSimulatorShaders(unittest.TestCase):
    def test_shader_literals_stay_concatenated(self):
        for name in ("vs", "fs"):
            block = shader_block(name)
            pieces = [
                line.strip()
                for line in block.splitlines()
                if line.strip().startswith('"')
            ]
            for piece in pieces[:-1]:
                self.assertTrue(
                    piece.endswith("+"),
                    f"{name}: literal is not joined to the next one: {piece[:60]}…",
                )
            self.assertTrue(pieces[-1].endswith(";"), f"{name}: block does not end")

    def test_fragment_shader_reaches_its_output(self):
        self.assertIn("gl_FragColor", shader_block("fs"))

    def test_ground_grid_is_drawn(self):
        source = PAGE.read_text(encoding="utf-8")
        self.assertIn("drawGrid();", source)


if __name__ == "__main__":
    unittest.main()
