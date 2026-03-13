from supadata import Supadata
import json

# --- SETTINGS ---
API_KEY = "PASTE_YOUR_SUPADATA_KEY_HERE"
VIDEO_URL = "https://www.youtube.com/watch?v=33DVEOwb9i0"

def test_sdk():
    print(f"[*] Initializing Supadata SDK...")
    try:
        client = Supadata(api_key=API_KEY)
        
        print(f"[*] Requesting transcript for {VIDEO_URL}...")
        # Official SDK method
        transcript = client.youtube.get_transcript(url=VIDEO_URL)
        
        print("\n--- SDK SUCCESS ---")
        print(f"Retrieved {len(transcript)} segments.")
        
        if len(transcript) > 0:
            # Let's see what is actually inside a segment object
            first = transcript[0]
            print(f"Keys found in segment: {first.__dict__.keys() if hasattr(first, '__dict__') else 'Standard List'}")
            
            # Use attributes or keys based on what the SDK returns
            text = getattr(first, 'text', 'N/A')
            start = getattr(first, 'start', 'N/A')
            print(f"First segment: [{start}] {text}")
            
    except Exception as e:
        print(f"\n--- SDK FAILURE ---")
        print(f"Error: {e}")

if __name__ == "__main__":
    test_sdk()
