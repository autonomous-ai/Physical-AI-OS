#!/usr/bin/env python3
"""Call a linked service with its stored credential, without the agent ever
handling the secret.

The agent names a connector, a method and a URL; this helper reads the token
from the device's connector config, attaches it, sends the request, and prints
the response body. The token never appears on a command line, in the process
list, or in anything the agent writes or reads.

Usage:
  connector.py list
  connector.py info <code>
  connector.py call <code> <METHOD> <url> [options]

Options for `call`:
  --query K=V        URL query parameter, URL-encoded for you (repeatable)
  --json BODY        JSON request body; `-` reads it from stdin
  --data K=V         form field, sent application/x-www-form-urlencoded (repeatable)
  --form K=V         multipart field; `K=@/path` uploads a file (repeatable)
  --header K:V       extra request header (repeatable; Authorization is refused)
  --token-param NAME send the credential as query parameter NAME instead of a
                     header (repeatable; only for endpoints that reject the header)

Exit codes: 0 success (2xx) · 1 HTTP error or network failure · 2 usage error ·
3 not connected / unusable credential · 4 host not allowed for this connector.
On any failure nothing is written to stdout, so a following `| jq` sees empty
input instead of an error body it could mistake for an empty result.
"""
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CONFIGS_DIR = Path(os.environ.get("CONNECTOR_CONFIGS_DIR", "/root/.openclaw/workspace/configs"))
TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# Official API hosts per connector. A credential is only ever sent to one of
# these (exact host or a subdomain of it). Connectors not listed here may call
# any HTTPS host; the skill tells the agent which host is official.
OFFICIAL_HOSTS = {
    "gmail": ("googleapis.com",),
    "google_calendar": ("googleapis.com",),
    "google_drive": ("googleapis.com",),
    "facebook": ("graph.facebook.com",),
    "figma": ("api.figma.com",),
    "github": ("api.github.com",),
    "ahrefs": ("api.ahrefs.com",),
}
GOOGLE_CODES = ("gmail", "google_calendar", "google_drive")


class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fmt_time(ts):
    """Local time plus the zone, so nobody reads a UTC stamp as local."""
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(int(ts)))


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Failure(3, f"cannot read {path.name}: {type(e).__name__}")


def load_entry(code):
    """Return the stored entry for a connector, or None when it is not linked."""
    data = load_json(CONFIGS_DIR / f"{code}_access_tokens.json")
    entry = ((data or {}).get("connectors") or {}).get(code)
    if entry:
        return entry
    data = load_json(CONFIGS_DIR / "connectors.json")
    return ((data or {}).get("connectors") or {}).get(code)


def account_of(entry):
    creds = entry.get("credentials") or {}
    if entry.get("auth_type") == "pat" and creds.get("email"):
        return creds["email"]
    return entry.get("user_email") or creds.get("email") or ""


def cmd_list():
    """Print every linked connector from all three credential shapes."""
    found = 0
    problems = []
    for path in sorted(CONFIGS_DIR.glob("*_access_tokens.json")):
        code = path.name[: -len("_access_tokens.json")]
        try:
            entry = ((load_json(path) or {}).get("connectors") or {}).get(code)
        except Failure as e:
            problems.append(str(e))
            continue
        if entry:
            found += 1
            print(describe(code, entry))
    for name, key, label in (("connectors.json", "connectors", ""), ("access_tokens.json", "providers", "provider ")):
        try:
            data = load_json(CONFIGS_DIR / name) or {}
        except Failure as e:
            problems.append(str(e))
            continue
        for code, entry in (data.get(key) or {}).items():
            found += 1
            if label:
                email = entry.get("user_email")
                print(f"provider {code}: oauth token present" + (f" ({email})" if email else ""))
            else:
                print(describe(code, entry))
    for p in problems:
        print(f"verification failed: {p}", file=sys.stderr)
    if problems:
        return 3
    if not found:
        print("no connectors linked")
    return 0


def describe(code, entry):
    parts = [account_of(entry), entry.get("auth_type") or "oauth"]
    return f"{code}: connected (" + ", ".join(p for p in parts if p) + ")"


def cmd_info(code):
    entry = load_entry(code)
    if not entry:
        raise Failure(3, f"{code}: not connected")
    expires = int(entry.get("expires_at") or 0)
    info = {
        "connector": code,
        "auth_type": entry.get("auth_type") or "oauth",
        "account": account_of(entry),
        "scopes": entry.get("scopes") or [],
        "auto_refresh": bool(entry.get("refresh_token")) and bool(entry.get("refresh")),
        "expires": "never" if not expires else fmt_time(expires),
        "expired": bool(expires) and expires < time.time(),
        "obtained": fmt_time(entry.get("obtained_at")),
    }
    page_id = (entry.get("credentials") or {}).get("page_id")
    if page_id:
        info["page_id"] = page_id
    print(json.dumps(info, indent=2))
    return 0


def host_allowed(code, host):
    allowed = OFFICIAL_HOSTS.get(code)
    if not allowed:
        return True
    return any(host == h or host.endswith("." + h) for h in allowed)


def pair(value, sep, flag):
    if sep not in value:
        raise Failure(2, f"{flag} expects K{sep}V, got {value!r}")
    k, v = value.split(sep, 1)
    return k.strip(), v if sep == "=" else v.strip()


def multipart(fields):
    boundary = uuid.uuid4().hex
    chunks = []
    for key, value in fields:
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
        if value.startswith("@"):
            path = Path(value[1:])
            try:
                content = path.read_bytes()
            except OSError as e:
                raise Failure(2, f"cannot read upload {path}: {type(e).__name__}")
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            head += f'; filename="{path.name}"\r\nContent-Type: {ctype}'
        else:
            content = value.encode()
        chunks += [head.encode() + b"\r\n\r\n", content, b"\r\n"]
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def parse_call_args(args):
    opts = {"query": [], "json": None, "data": [], "form": [], "header": [], "token_param": []}
    i = 0
    while i < len(args):
        flag = args[i]
        if i + 1 >= len(args):
            raise Failure(2, f"{flag} needs a value")
        value = args[i + 1]
        if flag == "--query":
            opts["query"].append(pair(value, "=", flag))
        elif flag == "--data":
            opts["data"].append(pair(value, "=", flag))
        elif flag == "--form":
            opts["form"].append(pair(value, "=", flag))
        elif flag == "--header":
            opts["header"].append(pair(value, ":", flag))
        elif flag == "--json":
            opts["json"] = sys.stdin.read() if value == "-" else value
        elif flag == "--token-param":
            opts["token_param"].append(value)
        else:
            raise Failure(2, f"unknown option {flag}")
        i += 2
    if sum(bool(x) for x in (opts["json"] is not None, opts["data"], opts["form"])) > 1:
        raise Failure(2, "use only one of --json, --data, --form")
    return opts


def cmd_call(code, method, url, args):
    opts = parse_call_args(args)
    method = method.upper()

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise Failure(4, f"refusing {url!r}: only https:// URLs are allowed")
    if not host_allowed(code, parsed.hostname):
        allowed = ", ".join(OFFICIAL_HOSTS[code])
        raise Failure(4, f"refusing to send the {code} credential to {parsed.hostname}; allowed: {allowed}")

    entry = load_entry(code)
    if not entry:
        raise Failure(3, f"{code}: not connected — ask the user to link it in the Autonomous app")
    if code in GOOGLE_CODES and entry.get("auth_type") == "pat":
        raise Failure(3, f"{code}: linked with an app password; Google REST APIs reject it — use IMAP/SMTP (see SKILL.md)")
    token = entry.get("access_token") or entry.get("api_key")
    if not token:
        raise Failure(3, f"{code}: linked but no usable credential stored")

    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) + opts["query"]
    for name in opts["token_param"]:
        query.append((name, token))
    full_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), "")
    )

    headers = {"Accept": "application/json"}
    for k, v in opts["header"]:
        if k.lower() == "authorization":
            raise Failure(2, "do not pass Authorization; the helper adds the credential itself")
        headers[k] = v
    if not opts["token_param"]:
        headers["Authorization"] = " ".join(("Bearer", token))

    body = None
    if opts["json"] is not None:
        body = opts["json"].encode()
        headers.setdefault("Content-Type", "application/json")
    elif opts["data"]:
        body = urllib.parse.urlencode(opts["data"]).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif opts["form"]:
        body, headers["Content-Type"] = multipart(opts["form"])

    req = urllib.request.Request(full_url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            out = resp.read(MAX_RESPONSE_BYTES)
            status = resp.status
    except urllib.error.HTTPError as e:
        detail = e.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        print(f"HTTP {e.code} from {parsed.hostname}", file=sys.stderr)
        if detail.strip():
            print(scrub(detail, token), file=sys.stderr)
        if e.code == 401:
            expires = int(entry.get("expires_at") or 0)
            print(
                f"credential: expires {fmt_time(expires) if expires else 'never'}, "
                f"auto_refresh={'yes' if entry.get('refresh_token') and entry.get('refresh') else 'no'}",
                file=sys.stderr,
            )
        return 1
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        print(f"request to {parsed.hostname} failed: {type(reason).__name__}", file=sys.stderr)
        return 1

    text = out.decode("utf-8", "replace")
    if text:
        sys.stdout.write(scrub(text, token))
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    elif status == 204:
        print("{}")
    return 0


def scrub(text, token):
    return text.replace(token, "[credential]") if token else text


def main(argv):
    try:
        if len(argv) >= 1 and argv[0] == "list":
            return cmd_list()
        if len(argv) == 2 and argv[0] == "info":
            return cmd_info(argv[1])
        if len(argv) >= 4 and argv[0] == "call":
            return cmd_call(argv[1], argv[2], argv[3], argv[4:])
        print(__doc__.split("\n\n", 2)[2], file=sys.stderr)
        return 2
    except Failure as e:
        print(str(e), file=sys.stderr)
        return e.code
    except BrokenPipeError:
        raise
    except Exception as e:  # never let a traceback echo request details
        print(f"connector helper failed: {type(e).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
        sys.stdout.flush()
    except BrokenPipeError:
        # The reader (e.g. a failing `| jq`) closed the pipe; its own error is
        # the one worth showing, not a Python traceback.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = 1
    sys.exit(code)
