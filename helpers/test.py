import os
import sys
import requests
import http.cookiejar
from youtube_transcript_api import YouTubeTranscriptApi

# --- SETTINGS ---
COOKIE_FILE = 'cookies.Personal.txt'
VIDEO_ID = '33DVEOwb9i0'

def test():
    print(f"[*] Starting diagnostics for Video: {VIDEO_ID}")
    
    # 1. Prepare Session
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36'
    })

    # 2. Load Cookies
    if not os.path.exists(COOKIE_FILE):
        print(f"[!] FAILED: {COOKIE_FILE} not found.")
        return

    try:
        cj = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
        cj.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(cj)
        
        # Verify the session actually has YouTube cookies now
        yt_cookies = [c.name for c in session.cookies if 'google' in c.domain or 'youtube' in c.domain]
        if not yt_cookies:
            print("[!] WARNING: Cookie file loaded, but no YouTube/Google domain cookies found.")
        else:
            print(f"[✓] Session Loaded with {len(yt_cookies)} auth cookies.")
    except Exception as e:
        print(f"[!] FAILED to parse cookie file: {e}")
        return

    # 3. Request Transcript
    print("[*] Calling YouTubeTranscriptApi.fetch()...")
    try:
        # Initializing per your doc.txt (http_client argument)
        api = YouTubeTranscriptApi(http_client=session)
        
        # Fetching per your doc.txt
        fetched = api.fetch(VIDEO_ID)
        
        print("\n--- SUCCESS ---")
        print(f"Total Segments: {len(fetched)}")
        # Accessing attributes .start and .text per FetchedTranscriptSnippet in doc.txt
        print(f"First line at {fetched[0].start}s: {fetched[0].text}")
        
    except Exception as e:
        print("\n--- FETCH FAILED ---")
        print(f"Error Type: {type(e).__name__}")
        print(f"Message: {e}")
        
        # Check specifically for IP Blocks
        if "RequestBlocked" in type(e).__name__ or "IpBlocked" in type(e).__name__:
            print("\n[CRITICAL] YouTube is blocking your VPS IP address regardless of cookies.")
        elif "Cookie" in type(e).__name__:
            print("\n[CRITICAL] YouTube rejected your cookie session. They may be expired.")

if __name__ == "__main__":
    test()
