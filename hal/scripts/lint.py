#!/usr/bin/env python3
"""HAL lint: catch broken local imports and undefined names that py_compile misses.

Upstream LeLamp core files (KEEP) are excluded. Run: `make hal-lint`.
"""
import ast
import os
import subprocess
import sys

HAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # hal
# Upstream LeLamp core kept verbatim; inherited undefined names must not be "fixed".
KEEP = {"main.py", "smooth_animation.py"}


def py_files():
    out = []
    for root, dirs, files in os.walk(HAL):
        if ".venv" in root or "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return out


def _mod_exists(base, parts):
    p = os.path.join(base, *parts)
    return os.path.isfile(p + ".py") or os.path.isdir(p)


_LOCAL_TOPS = {
    e[:-3] if e.endswith(".py") else e
    for e in os.listdir(HAL)
    if e.endswith(".py") or os.path.isdir(os.path.join(HAL, e))
}

_REPO = os.path.dirname(HAL)  # parent of hal/ — where the `hal.` package roots
_STDLIB = getattr(sys, "stdlib_module_names", frozenset())


def _build_module_index():
    """basename -> [hal.-dotted paths that provide it], to detect moved local modules."""
    idx = {}
    for root, dirs, files in os.walk(HAL):
        if ".venv" in root or "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py") and f != "__init__.py":
                base = f[:-3]
                dotted = os.path.relpath(os.path.join(root, base), _REPO).replace(os.sep, ".")
                idx.setdefault(base, []).append(dotted)
    return idx


_HAL_MODULES = _build_module_index()


def _defined_names(path):
    """Top-level names a module exports (class/def/assign/import), for FP-safe matching."""
    try:
        tree = ast.parse(open(path).read(), path)
    except (SyntaxError, OSError):
        return set()
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
    return names


def check_imports(files):
    bad = []
    for path in files:
        try:
            tree = ast.parse(open(path).read(), path)
        except SyntaxError as e:
            bad.append(f"{path}: SYNTAX {e}")
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.ImportFrom):
                continue
            if n.level:
                base = os.path.dirname(path)
                for _ in range(n.level - 1):
                    base = os.path.dirname(base)
                if n.module and not _mod_exists(base, n.module.split(".")):
                    bad.append(f"{path}:{n.lineno}: from {'.' * n.level}{n.module} -> module not found")
            elif n.module:
                parts = n.module.split(".")
                if parts[0] == "hal" and not _mod_exists(HAL, parts[1:]):
                    bad.append(f"{path}:{n.lineno}: from {n.module} -> not found under hal")
                elif parts[0] in _LOCAL_TOPS and not _mod_exists(HAL, parts):
                    bad.append(f"{path}:{n.lineno}: from {n.module} -> not found under hal")
                elif parts[0] not in ("hal", *_LOCAL_TOPS) and parts[0] not in _STDLIB:
                    # May be a moved local module imported by its old bare path; flag only if a hal module
                    # of that basename defines every imported name. Stdlib is excluded.
                    imported = {a.name for a in n.names}
                    for dotted in _HAL_MODULES.get(parts[-1], []):
                        cand = os.path.join(_REPO, *dotted.split(".")) + ".py"
                        if imported and imported <= _defined_names(cand):
                            bad.append(f"{path}:{n.lineno}: from {n.module} -> stale/moved local import; use {dotted}")
                            break
    return bad


def check_undefined(files):
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        print(
            "⚠ pyflakes not installed — skipping undefined-name check "
            "(install: cd hal && uv sync --extra dev)",
            file=sys.stderr,
        )
        return []
    res = subprocess.run(
        [sys.executable, "-m", "pyflakes", *files], capture_output=True, text=True
    )
    bad = []
    for line in (res.stdout + res.stderr).splitlines():
        if "undefined name" not in line:
            continue
        if os.path.basename(line.split(":", 1)[0]) in KEEP:
            continue
        bad.append(line)
    return bad


def main():
    files = py_files()
    problems = check_imports(files) + check_undefined(files)
    if problems:
        print("✗ HAL lint found refactor-leftover bugs (runtime Import/NameError):")
        for p in sorted(problems):
            print("  " + p)
        return 1
    print(
        f"✓ HAL lint clean — {len(files)} files: no broken local imports / "
        f"undefined names (upstream-keep {', '.join(sorted(KEEP))} excluded)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
