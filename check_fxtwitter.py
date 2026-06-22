import requests

tweet_id = "1678128919655612416"
endpoints = [
    f"https://api.vxtwitter.com/status/{tweet_id}",
    f"https://api.vxtwitter.com/Twitter/status/{tweet_id}",
    f"https://api.fxtwitter.com/status/{tweet_id}",
    f"https://api.fxtwitter.com/Twitter/status/{tweet_id}",
    f"https://api.fixupx.com/status/{tweet_id}",
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
                print(f"JSON Output (keys): {list(resp.json().keys())}")
                print(f"JSON Snippet: {str(resp.json())[:300]}")
            except Exception as je:
                print(f"Failed to parse JSON: {je}")
                print(f"Text Snippet: {resp.text[:300]}")
        else:
            print(f"Text Snippet: {resp.text[:300]}")
    except Exception as e:
        print(f"Request failed: {e}")
    print("-" * 50)
