import requests
import json

# Your exact key from the logs
API_KEY = "AIzaSyC_kjoQ7GQj_PCdjU_X-Nw2Lwhr24jmcbQ"
URL = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={API_KEY}"

# The absolute simplest valid payload possible
payload = {
    "contents": [
        {
            "parts": [
                {"text": "Respond with the word SUCCESS in a JSON object."}
            ]
        }
    ]
}

print(f"[*] Sending request to: {URL}")
try:
    response = requests.post(URL, json=payload, timeout=30)
    print(f"[*] Status Code: {response.status_code}")
    print("[*] Raw Response Body:")
    print(response.text)
except Exception as e:
    print(f"[!] Connection Error: {e}")
