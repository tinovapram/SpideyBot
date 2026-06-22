import requests

tweet_id = "1585006311680581632"
url = f"https://api.fxtwitter.com/elonmusk/status/{tweet_id}"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

print(f"Requesting: {url}")
try:
    resp = requests.get(url, headers=headers, timeout=10)
    print(f"Status Code: {resp.status_code}")
    print(f"Headers: {resp.headers}")
    if resp.status_code == 200:
        js = resp.json()
        print(f"Success! Tweet keys: {list(js.keys())}")
        print(f"Tweet details: {js}")
    else:
        print(f"Failed with text: {resp.text}")
except Exception as e:
    print(f"Error: {e}")
