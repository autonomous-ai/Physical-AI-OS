"""Add structured tool/cache evidence to the known native Hermes Runs shape."""
import ast
import os
from pathlib import Path
import sys
import tempfile


class UnsupportedSource(ValueError):
    pass


LEGACY_USAGE = '''(
    ("input_tokens", "session_prompt_tokens"), ("output_tokens", "session_completion_tokens"),
    ("total_tokens", "session_total_tokens"))'''
FIXED_USAGE = LEGACY_USAGE[:-1] + ''',
    ("cache_read_tokens", "session_cache_read_tokens"),
    ("cache_write_tokens", "session_cache_write_tokens"))'''
LEGACY_CAPS = '''{
    "supported": True,
    "durable": self._run_idempotency_store.durable,
    "retention_seconds": store_type.RETENTION_SECONDS}'''
FIXED_CAPS = LEGACY_CAPS[:-1] + ', "autonomous_run_events_v1": True}'
LEGACY_CREATE = '''self._create_agent(
    stream_delta_callback=_text_cb, tool_progress_callback=self._make_run_event_callback(run_id, loop),
    **run.agent_kwargs)'''
FIXED_CREATE = '''self._create_agent(
    stream_delta_callback=_text_cb, tool_progress_callback=self._make_run_event_callback(run_id, loop),
    tool_start_callback=lambda call_id, name, args: _autonomous_run_tool_event(
        run, loop, "tool.call.started", call_id, name, args),
    tool_complete_callback=lambda call_id, name, args, result: _autonomous_run_tool_event(
        run, loop, "tool.call.completed", call_id, name, args, result),
    **run.agent_kwargs)'''
# Hermes 0.21.5 already reports cache usage and adds interim assistant output.
# Keep that callback intact while enriching the same native Runs transport.
MODERN_CREATE = LEGACY_CREATE.replace(
    "**run.agent_kwargs", "interim_assistant_callback=_interim_cb, **run.agent_kwargs")
FIXED_MODERN_CREATE = FIXED_CREATE.replace(
    "**run.agent_kwargs", "interim_assistant_callback=_interim_cb, **run.agent_kwargs")
HELPER = '''def _autonomous_run_tool_event(run, loop, event, call_id, name, arguments, result=None):
    """Observe existing tool callbacks without affecting execution or progress events."""
    with suppress(Exception):
        fields = {"tool_call_id": call_id, "tool": name, "arguments": arguments}
        if event == "tool.call.completed":
            fields["result"] = result
        payload = _run_event(run.run_id, event, **fields)
        loop.call_soon_threadsafe(run.put_event, payload)
'''


def same(left, right):
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def expr(source):
    return ast.parse(source, mode="eval").body


def patched_source(source):
    tree = ast.parse(source)
    raw = source.encode("utf-8")
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    edits = []
    usage = [node.value for node in tree.body if isinstance(node, ast.Assign)
             and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
             and node.targets[0].id == "_USAGE_FIELDS"]
    functions = {}
    for name in ("_idempotency_capabilities", "_execute_run"):
        found = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name == name]
        if len(found) != 1:
            raise UnsupportedSource(f"expected one {name}")
        functions[name] = found[0]
    caps = [node.value for node in functions["_idempotency_capabilities"].body if isinstance(node, ast.Return)]
    creates = [node for node in ast.walk(functions["_execute_run"]) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute) and node.func.attr == "_create_agent"]
    modern = len(creates) == 1 and any(
        same(creates[0], expr(shape)) for shape in (MODERN_CREATE, FIXED_MODERN_CREATE))
    states = []
    for nodes, legacy, fixed in ((usage, LEGACY_USAGE, FIXED_USAGE),
                                 (caps, LEGACY_CAPS, FIXED_CAPS),
                                 (creates, MODERN_CREATE if modern else LEGACY_CREATE,
                                  FIXED_MODERN_CREATE if modern else FIXED_CREATE)):
        if len(nodes) != 1:
            raise UnsupportedSource("unsupported native Runs target count")
        node = nodes[0]
        if same(node, expr(fixed)):
            states.append("fixed")
            continue
        if not same(node, expr(legacy)):
            raise UnsupportedSource("unsupported native Runs source shape")
        states.append("legacy")
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        replacement = fixed.replace("\n", "\n" + " " * node.col_offset).encode("utf-8")
        edits.append((start, end, replacement))
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name == "_autonomous_run_tool_event"]
    if states == ["fixed"] * 3:
        if len(helpers) != 1 or not same(helpers[0], ast.parse(HELPER).body[0]):
            raise UnsupportedSource("unverified native Runs helper")
        return source
    expected = ["fixed", "legacy", "legacy"] if modern else ["legacy"] * 3
    if states != expected or helpers:
        raise UnsupportedSource("partially patched native Runs source")
    for start, end, replacement in sorted(edits, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    updated = raw.decode("utf-8") + "\n\n" + HELPER
    compile(updated, "api_server_runs.py", "exec")
    return updated


def patch_file(path):
    path = Path(path)
    if not path.exists():
        return False
    if path.is_symlink():
        raise UnsupportedSource("API source must not be a symlink")
    original = path.read_bytes()
    updated = patched_source(original.decode("utf-8")).encode("utf-8")
    if updated == original:
        return False
    stat = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=".native-runs-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), stat.st_mode & 0o7777)
            if (os.fstat(stream.fileno()).st_uid, os.fstat(stream.fileno()).st_gid) != (stat.st_uid, stat.st_gid):
                os.fchown(stream.fileno(), stat.st_uid, stat.st_gid)
        if path.read_bytes() != original:
            raise UnsupportedSource("API source changed while patching")
        backup = path.with_name(path.name + ".autonomous-runs-v1.bak")
        if not backup.exists():
            with backup.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), stat.st_mode & 0o7777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


if __name__ == "__main__":
    try:
        print("changed" if patch_file(sys.argv[1]) else "unchanged")
    except (OSError, SyntaxError, UnsupportedSource) as error:
        print(f"Hermes native Runs patch refused: {error}", file=sys.stderr)
        sys.exit(1)
