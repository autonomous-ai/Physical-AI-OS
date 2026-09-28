# Continuous integration

`.github/workflows/ci.yml` runs Go build/vet/tests, web build/lint, and Python
checks on pushes and pull requests. Python uses 3.12, matching the HAL runtime.

Run the Python source gate locally with:

```bash
python3 -B scripts/ci/check_python_syntax.py
python3 -B -m unittest discover -s scripts/ci -p 'test_*.py' -v
python3 -B -m unittest hal.test.test_route_contracts -v
python3 -B -m unittest discover -s runtimes/hermes -p '*_test.py' -v
```

The syntax gate compiles **every Git-tracked `.py` file**, including new HAL
routes, without importing hardware dependencies or writing bytecode. Add new
files to Git before running it locally. Invalid decorator syntax fails the
gate; a decorator that is syntactically valid but fails at runtime still needs
an import or behavior test.

The `/emotion` declaration contract also verifies that the registered POST
handler requires `EmotionRequest` and declares `EmotionResponse`. It catches
the historical misplaced decorator on `harness_blocks_sleep` even though that
source compiles. This is a targeted contract, not an import test of every route.

Embedded Hermes Python patches need a root-level `<patch_stem>_test.py` suite
under `runtimes/hermes/`. CI rejects newly embedded `*_patch.py` scripts or
scripts in `patches/` without a discoverable, non-empty suite. These suites
must apply patches to upstream-shaped fixtures, compile the resulting source,
verify repeated application, and check unsupported anchors. Compiling the
patch script alone cannot validate Python held in its replacement strings.

Channel configuration contract tests also run under `go test ./system/device`.
They check that empty and unrelated config updates preserve existing channel
settings, including new fields/channels discovered by the test. They do not
replace setup, credential delivery, or end-to-end channel tests in feature PRs.

Passing CI establishes the covered contracts, not freedom from all bugs or
compatibility with every future upstream Hermes revision. Changes to workflows
take effect when those changes are included in the tested branch/merge; an old
green run is not evidence that the new gates ran.
