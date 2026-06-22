import re
import requests

url = "https://twitter.com/NASA/status/1678128919655612416"

# Extract username and tweet ID
match = re.search(r"(?:twitter|x)\.com/([^/]+)/(?:web|status(?:es)?)/([0-9]+)", url)
if match:
    username = match.group(1)
    tweet_id = match.group(2)
else:
    username = "i"
    tweet_id = "1678128919655612416"

print(f"Extracted Username: {username}, Tweet ID: {tweet_id}")

endpoints = [
    f"https://api.fxtwitter.com/{username}/status/{tweet_id}",
    f"https://api.fxtwitter.com/i/status/{tweet_id}",
    f"https://api.vxtwitter.com/{username}/status/{tweet_id}",
    f"https://api.vxtwitter.com/i/status/{tweet_id}",
]

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

for ep in endpoints:
    print(f"Testing endpoint: {ep}")
    try:
        resp = requests.get(ep, headers=headers, timeout=10)
        print(f"Status Code: {resp.status_code}")
        print(f"Content-Type: {resp.headers.get('Content-Type')}")
        if resp.status_code == 200:
            try:
                js = resp.json()
                print(f"Success! Tweet text: {js.get('tweet', {}).get('text')}")
                print(f"Media: {js.get('tweet', {}).get('media')}")
            except Exception as je:
                print(f"Failed to parse JSON: {je}")
                print(f"Text Snippet: {resp.text[:300]}")
        else:
            print(f"Text Snippet: {resp.text[:300]}")
    except Exception as e:
        print(f"Request failed: {e}")
    print("-" * 50)
