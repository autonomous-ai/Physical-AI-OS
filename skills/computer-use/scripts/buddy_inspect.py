"""Read-only desktop observations through Cua or native Accessibility."""

import math
import re
import time

MAX_ITEMS = 120
TEXT_LIMIT = 240


def compact_tree(tree, navigation=False):
    """Keep observed references and text, with explicit incompleteness markers."""
    nodes = tree.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > 500:
        raise ValueError("invalid Accessibility nodes")
    by_ref = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("ref"), str) or not node["ref"] or node["ref"] in by_ref:
            raise ValueError("invalid or duplicate Accessibility reference")
        by_ref[node["ref"]] = node
    items = []
    omitted = 0
    text_clipped = False
    # Navigation prioritizes observed enabled controls while keeping their
    # original refs/parents. Stable ordering preserves peer order within groups.
    ordered = sorted(nodes, key=lambda node: not (node.get("enabled") is True and node.get("actions"))) if navigation else nodes
    for node in ordered:
        current, seen = node, set()
        hidden = False
        while current:
            ref = current["ref"]
            if ref in seen:
                raise ValueError("cyclic Accessibility ancestry")
            seen.add(ref)
            # Missing privacy markers are unknown, never permission to expose text.
            hidden |= current.get("secure") is not False or (not navigation and current.get("role") in ("AXMenuBar", "AXMenu", "AXMenuItem"))
            parent = current.get("parent_ref")
            if not parent:
                break
            if parent not in by_ref:
                raise ValueError("missing Accessibility parent")
            current = by_ref[parent]
        if hidden:
            omitted += 1
            continue
        item = {key: node[key] for key in ("ref", "parent_ref", "role", "enabled", "focused", "actions") if key in node}
        has_text = False
        for key in ("title", "description", "value"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                item[key] = value[:TEXT_LIMIT]
                has_text = True
                text_clipped |= len(value) > TEXT_LIMIT
        if not has_text and not node.get("actions"):
            omitted += 1
            continue
        if len(items) >= MAX_ITEMS:
            omitted += 1
            continue
        items.append(item)
    result = {key: tree.get(key) for key in ("app", "bundle_id", "snapshot_id", "expires_in_ms", "frontmost", "truncated")}
    result.update(items=items, observed_nodes=len(nodes), omitted_nodes=omitted,
                  text_clipped=text_clipped, compact=True)
    issues = []
    if tree.get("frontmost") is not True:
        issues.append("app_not_frontmost")
    if tree.get("truncated") is not False:
        issues.append("tree_incomplete")
    if omitted or text_clipped:
        issues.append("compact_output_omits_content")
    result["issues"] = issues
    return result


def _frame(value):
    """Accept finite observed rectangles only, without coordinate conversion."""
    if not isinstance(value, dict):
        return None
    values = [value.get(key) for key in ("x", "y", "w", "h")]
    try:
        if any(type(number) not in (int, float) or not math.isfinite(number) for number in values):
            return None
        x, y, width, height = values
        if width <= 0 or height <= 0 or not math.isfinite(x + width) or not math.isfinite(y + height):
            return None
    except OverflowError:
        return None
    return x, y, x + width, y + height


def compact_cua(observation):
    """Preserve evidence and annotate geometry; never claim hit-test visibility."""
    # Driver max_elements advice is incompatible with Buddy's max_nodes. Keep
    # tree_markdown: static text can exist there alone, including calendar year.
    result = {key: value for key, value in observation.items() if key != "_note"}
    elements = observation.get("elements")
    if not isinstance(elements, list):
        return result
    windows = [element for element in elements if isinstance(element, dict) and element.get("role") == "AXWindow"]
    window = _frame(windows[0].get("frame")) if len(windows) == 1 else None
    by_index, duplicate = {}, set()
    for element in elements:
        if isinstance(element, dict) and type(element.get("element_index")) is int:
            index = element["element_index"]
            if index in by_index:
                duplicate.add(index)
            by_index[index] = element
    menu_roles = {"AXMenu", "AXMenuBar", "AXMenuItem", "AXMenuBarItem"}
    annotated = []
    modal = False
    for element in elements:
        if not isinstance(element, dict):
            annotated.append(element)
            continue
        modal |= element.get("role") == "AXSheet" or element.get("modal") is True
        current, seen, menu, ancestry_unknown = element, set(), False, False
        while current is not None:
            if current.get("role") in menu_roles:
                menu = True
                break
            parent = current.get("parent_index")
            if parent is None:
                break
            if type(parent) is not int or parent in seen or parent in duplicate or parent not in by_index:
                ancestry_unknown = True
                break
            seen.add(parent)
            current = by_index[parent]
        bounds = _frame(element.get("frame"))
        geometry = "unknown"
        if not menu and not ancestry_unknown and window and bounds:
            x, y, right, bottom = bounds
            left, top, window_right, window_bottom = window
            if right <= left or bottom <= top or x >= window_right or y >= window_bottom:
                geometry = "outside_window"
            elif x >= left and y >= top and right <= window_right and bottom <= window_bottom:
                geometry = "inside_window"
            else:
                geometry = "partially_visible"
        copy = dict(element, window_geometry=geometry)
        if menu:
            copy["window_geometry_exemption"] = "menu"
        annotated.append(copy)
    result["elements"] = annotated
    result["geometry_note"] = "Window geometry is not proof of visibility or clickability. Avoid targeting outside-window elements; menus are exempt."
    if modal:
        result["interaction_hint"] = "An AXSheet or modal element is observed. Inspect and handle that dialog before interacting with the underlying window."
    return result


def validate_inspect_params(params):
    """Validate observation input before any command, including a preceding action."""
    if not isinstance(params, dict) or set(params) - {"app", "window_id", "mode"}:
        raise ValueError("inspect params may contain only app, window_id and mode")
    if params.get("mode", "auto") not in ("auto", "navigation", "detail"):
        raise ValueError("inspect mode must be auto, navigation or detail")
    if "app" in params and (not isinstance(params["app"], str) or not params["app"].strip() or len(params["app"]) > 256):
        raise ValueError("app must contain 1–256 characters")
    if "window_id" in params and (type(params["window_id"]) is not int or params["window_id"] <= 0):
        raise ValueError("window_id must be a positive integer")


def inspect_ui(params, command, known_backend=None):
    """Observe one backend; incomplete auto trees get one fresh navigation view."""
    validate_inspect_params(params)
    if known_backend not in (None, "cua", "native"):
        raise ValueError("invalid known observation backend")
    if known_backend == "native" and "window_id" in params:
        raise ValueError("window_id requires Cua")
    started = time.monotonic()
    requests = []

    def measured_command(action, target):
        before = time.monotonic()
        response = command(action, target)
        requests.append({"action": action, "id": response.get("id"),
                         "elapsed_ms": round((time.monotonic() - before) * 1000, 2)})
        return response

    if known_backend is None:
        result = _inspect_ui(params, measured_command)
    else:
        # A successful typed action proves its backend. The companion still
        # enforces pause and permissions on this new observation command.
        result = _observe_backend(params, measured_command, known_backend, None)
        result.update(backend=known_backend, backend_source="successful_action")

    result["timing"] = {"elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                        "requests": requests}
    return result


def _inspect_ui(params, command):
    desktop = command("desktop_info", {}).get("result")
    if not isinstance(desktop, dict):
        raise ValueError("invalid desktop information")
    state = {key: desktop.get(key) for key in ("paused", "accessibility", "screen_recording", "frontmost_app", "cua",
                                                       "capabilities", "protocol_version")}
    if desktop.get("paused") is not False:
        return {"ok": False, "reason": "desktop_paused_or_unknown", "desktop": state}
    cua = desktop.get("cua")
    if isinstance(cua, dict) and cua.get("enabled") is True and cua.get("installed") is True:
        # Cua has its own TCC identity. Buddy's AX grant cannot predict its readiness.
        return _observe_backend(params, command, "cua", state)
    if "window_id" in params:
        raise ValueError("window_id requires Cua")
    if desktop.get("accessibility") is not True:
        return {"ok": False, "reason": "accessibility_unavailable", "desktop": state}
    return _observe_backend(params, command, "native", state)


def _observe_backend(params, command, backend, desktop):
    """Never mix snapshots: a navigation overview replaces the incomplete tree."""
    mode = params.get("mode", "auto")
    target = {key: value for key, value in params.items() if key != "mode"}
    action = "cua_observe" if backend == "cua" else "get_ui_tree"

    def capture(navigation):
        arguments = dict(target)
        if navigation:
            arguments.update(max_nodes=500, max_depth=2 if backend == "cua" else 4)
        elif backend == "native" or mode == "detail":
            arguments.update(max_nodes=500, max_depth=12)
        raw = command(action, arguments).get("result")
        if not isinstance(raw, dict):
            raise ValueError("invalid Cua observation" if backend == "cua" else "invalid Accessibility result")
        # Validate native trees before deciding whether to request another view.
        observation = compact_cua(raw) if backend == "cua" else compact_tree(raw, navigation=navigation)
        incomplete = raw.get("truncated") is True
        if backend == "cua":
            # elements_complete=false can mean static text exists only in the
            # markdown; it does not establish that the AX traversal was cut.
            markdown = raw.get("tree_markdown")
            footer = markdown.rstrip().split("\n")[-1] if isinstance(markdown, str) and markdown.strip() else ""
            incomplete |= raw.get("tree_truncated") is True or bool(
                re.fullmatch(r"\s*(?:⚠️?\s+)?AX tree truncated at \d+ (?:nodes|elements)(?:[ .(][^\n]*)?", footer)
            )
        return observation, incomplete

    navigation = mode == "navigation"
    observation, incomplete = capture(navigation)
    if mode == "auto" and incomplete:
        # This is one bounded read of a different tree depth, never a retry of
        # an errored command or an action. Only these fresh references survive.
        observation, _ = capture(True)
        navigation = True
    if backend == "native" and desktop is not None and desktop.get("screen_recording") is not True:
        observation["issues"].append("screen_recording_unavailable")
    result = {"ok": True, "desktop": desktop, "observation": observation,
              "observation_mode": "navigation" if navigation else "detail",
              "navigation_only": navigation}
    if navigation:
        result["content_complete"] = False
        result["notice"] = "Navigation overview only; inspect with mode detail to read content. Missing items do not establish absence."
    return result
