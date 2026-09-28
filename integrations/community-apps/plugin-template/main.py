"""Hello World plugin for Autonomous OS: rainbow LED, spoken greeting, then LED off."""

import os
import time

import requests

HAL = os.environ.get("HAL_URL", "http://localhost:5001")


def main():
    requests.post(f"{HAL}/led/effect", json={"effect": "rainbow"}, timeout=5)

    requests.post(
        f"{HAL}/voice/speak",
        json={"text": "Hello! I am a plugin running on Autonomous OS."},
        timeout=30,
    )

    time.sleep(30)

    requests.post(f"{HAL}/led/off", timeout=5)


if __name__ == "__main__":
    main()
