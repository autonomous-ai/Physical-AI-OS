"""Test AutonomousSTT WebSocket connection."""

import argparse
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DEFAULT_BASE_URL = "https://campaign-api.autonomous.ai/api/v1/ai/v1"
WAIT_SECONDS = 4


def main():
    parser = argparse.ArgumentParser(description="Test AutonomousSTT connection")
    parser.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""))
    parser.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default="nova-3")
    args = parser.parse_args()

    if not args.api_key:
        print("ERROR: provide --api-key or set LLM_API_KEY env var", file=sys.stderr)
        sys.exit(1)

    from hal.drivers.voice.stt import AutonomousSTT

    print(f"[test] base_url={args.base_url}  model={args.model}")

    provider = AutonomousSTT(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )
    session = provider.create_session()

    def on_transcript(text: str, is_final: bool):
        print(f"[transcript] final={is_final}  text={text!r}")

    print(f"[test] connecting...")
    ok = session.start(on_transcript)

    if ok:
        print(f"[test] CONNECTED — waiting {WAIT_SECONDS}s then closing")
        time.sleep(WAIT_SECONDS)
    else:
        print("[test] FAILED to connect — check logs above")

    session.close()
    print("[test] done")


if __name__ == "__main__":
    main()
