"""Compile every tracked Python source without imports, hardware, or pycache."""

from pathlib import Path
import subprocess
import sys
import tokenize


def check(root: Path) -> int:
    paths = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "*.py"], cwd=root
    ).decode().split("\0")
    checked = 0
    errors = 0
    for name in filter(None, paths):
        try:
            with tokenize.open(root / name) as source:
                compile(source.read(), name, "exec", dont_inherit=True)
            checked += 1
        except (SyntaxError, UnicodeError, OSError, ValueError) as exc:
            errors += 1
            print(f"{name}: {exc}", file=sys.stderr)
    print(f"Python syntax: {checked} passed, {errors} failed")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(check(Path(__file__).resolve().parents[2]))
