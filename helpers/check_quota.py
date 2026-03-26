#!/usr/bin/env python3
"""Test Gemini API key quota across the most commonly-used models.

Run this when ``TubeNews.py`` starts failing with HTTP 429 (rate limit)
errors on Gemini calls.  It sends a minimal one-word prompt to each model
in turn and reports whether the key has working quota.

Usage::

    python3 helpers/check_quota.py

If every model fails, the most likely cause is that the Google Cloud project
associated with this key has exhausted its free quota for the billing period.
Create a new project in Google AI Studio, generate a fresh key, and update
``gemini_api_key`` in ``TubeNews.json``.
"""

import json
import sys
from pathlib import Path

import requests

CONFIG_FILE = Path(__file__).resolve().parent.parent / "TubeNews.json"

# Models most likely to have free-tier quota, checked in preference order.
# Update this list if Google adds or removes models.
MODELS_TO_TEST = [
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1/models"
    "/{model}:generateContent?key={key}"
)


def test_models(api_key: str) -> str | None:
    """Send a minimal prompt to each model and return the first that responds.

    Args:
        api_key: Gemini API key read from ``TubeNews.json``.

    Returns:
        The name of the first working model, or ``None`` if all fail.
    """
    for model in MODELS_TO_TEST:
        url = GEMINI_URL.format(model=model, key=api_key)
        print(f"[*] Testing {model} …")
        try:
            res = requests.post(
                url,
                json={"contents": [{"parts": [{"text": "hi"}]}]},
                timeout=10,
            )
            if res.status_code == 200:
                reply = (
                    res.json()
                    .get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                    .strip()
                )
                print(f"    [✓] Working — model replied: {reply!r}")
                return model
            else:
                msg = res.json().get("error", {}).get("message", "unknown error")
                print(f"    [✗] HTTP {res.status_code}: {msg}")
        except Exception as exc:
            print(f"    [!] Request failed: {exc}")

    return None


def main() -> None:
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        sys.exit(f"Error: {CONFIG_FILE} not found — copy TubeNews.json.sample first.")
    except json.JSONDecodeError as exc:
        sys.exit(f"Error: could not parse {CONFIG_FILE}: {exc}")

    api_key = config.get("gemini_api_key", "")
    if not api_key:
        sys.exit("Error: gemini_api_key is not set in TubeNews.json.")

    winner = test_models(api_key)

    if winner:
        current = config.get("gemini_model", "")
        print(f"\n[✓] Use model: {winner!r}")
        if current and current != winner:
            print(
                f"    Note: TubeNews.json currently has gemini_model: {current!r}.\n"
                f"    Update it to {winner!r} if that model is no longer working."
            )
    else:
        print(
            "\n[✗] All models returned errors.\n"
            "    Go to https://aistudio.google.com, create a new project,\n"
            "    generate a fresh API key, and update gemini_api_key in TubeNews.json."
        )


if __name__ == "__main__":
    main()
