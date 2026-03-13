import requests
import json

# --- SETTINGS ---
API_KEY = "sd_7c9a217f4e70a2823b3308828b65289b"
VIDEO_ID = "33DVEOwb9i0" # The Gonzales meeting video

def test_fetch():
    print(f"[*] Requesting transcript for {VIDEO_ID} via Supadata...")
    
    # Supadata documentation uses this endpoint
    url = f"https://api.supadata.ai/v1/youtube/transcript?url=https://www.youtube.com/watch?v={VIDEO_ID}"
    
    headers = {
        "x-api-key": API_KEY
    }

    try:
        # We use a longer timeout because Supadata is doing the proxy work behind the scenes
        response = requests.get(url, headers=headers, timeout=60)
        
        if response.status_code == 200:
            data = response.json()
            
            # Supadata usually returns a list of segments in the 'content' key
            # or directly as a list. Let's inspect it.
            print("\n--- API SUCCESS ---")
            
            # Check for standard Supadata response structure
            content = data.get('content', [])
            if not content and isinstance(data, list):
                content = data
                
            if content:
                print(f"Retrieved {len(content)} segments.")
                # Print the first segment to verify the structure
                first = content[0]
                print(f"First line: [{first.get('start', '??')}] {first.get('text', '??')}")
            else:
                print("Warning: API returned 200 but content list is empty.")
                print(f"Full response: {data}")
                
        else:
            print(f"\n--- API FAILURE ---")
            print(f"Status Code: {response.status_code}")
            print(f"Response: {response.text}")

    except Exception as e:
        print(f"\n--- CONNECTION ERROR ---")
        print(f"Error: {e}")

if __name__ == "__main__":
    test_fetch()
