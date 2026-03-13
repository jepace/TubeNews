import requests
import json

# Your key from the logs
API_KEY = "AIzaSyC_kjoQ7GQj_PCdjU_X-Nw2Lwhr24jmcbQ"

def harvest():
    endpoints = [
        "https://generativelanguage.googleapis.com/v1/models",
        "https://generativelanguage.googleapis.com/v1beta/models"
    ]
    
    results = {}

    for url in endpoints:
        print(f"[*] Fetching from: {url}...")
        try:
            res = requests.get(f"{url}?key={API_KEY}", timeout=30)
            if res.status_code == 200:
                results[url] = res.json()
            else:
                results[url] = {
                    "ERROR_CODE": res.status_code,
                    "ERROR_BODY": res.text
                }
        except Exception as e:
            results[url] = {"EXCEPTION": str(e)}

    # Dump everything to a file so it's not truncated in your terminal
    with open("raw_model_dump.json", "w") as f:
        json.dump(results, f, indent=2)

    # Also print it to the screen
    print("\n--- RAW DATA DUMP ---")
    print(json.dumps(results, indent=2))
    print("\n--- DUMP COMPLETE ---")
    print("[*] Full results also saved to: raw_model_dump.json")

if __name__ == "__main__":
    harvest()
