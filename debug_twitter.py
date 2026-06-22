import requests
import re

url = "https://twitter.com/elonmusk/status/1585006311680581632"

# 2. Test savetwitter
print("\n--- TEST savetwitter ---")
endpoint = "https://savetwitter.net/api/ajaxSearch"
payload = {
    "q": url,
    "lang": "en",
    "cftoken": ""
}
headers = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://savetwitter.net",
    "Referer": "https://savetwitter.net/en4",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
try:
    resp = requests.post(endpoint, headers=headers, data=payload, timeout=15)
    print(f"Status Code: {resp.status_code}")
    print(f"Response Snippet: {resp.text[:1000]}")
except Exception as e:
    print(f"savetwitter failed: {e}")
