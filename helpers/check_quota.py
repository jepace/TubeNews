#!/usr/bin/env python3
import requests
import json
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent.parent / "TubeNews.json"

with open(CONFIG_FILE, 'r') as f:
    _config = json.load(f)

API_KEY = _config['gemini_api_key']

# These are the models from your Harvester dump most likely to have free quota
models_to_test = [
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash"
]

def test_models():
    for model in models_to_test:
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={API_KEY}"
        print(f"[*] Testing {model}...")
        try:
            res = requests.post(url, json={'contents': [{'parts': [{'text': 'hi'}]}]}, timeout=10)
            if res.status_code == 200:
                print(f"    [✓] SUCCESS! {model} is working and has quota.")
                print(f"    AI Response: {res.json()['candidates'][0]['content']['parts'][0]['text']}")
                return model # Found our winner
            else:
                print(f"    [X] FAILED ({res.status_code}): {res.json().get('error', {}).get('message', 'Unknown error')}")
        except Exception as e:
            print(f"    [!] Error: {e}")
    return None

if __name__ == "__main__":
    winner = test_models()
    if winner:
        print(f"\nWINNER FOUND: Use '{winner}' in your config file.")
    else:
        print("\n[!] All models returned 0 quota. Please go to AI Studio, create a BRAND NEW project, and get a new key. Sometimes old projects get stuck with 0 quota for new models.")
