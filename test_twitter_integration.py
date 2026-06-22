import os
import sys

# Ensure spideybot is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spideybot.downloaders.site_downloaders.twitter import TwitterDownloader

def test_download(url):
    print(f"Testing URL: {url}")
    td = TwitterDownloader()
    try:
        res = td.fetch_media(url)
        print("Fetch media result:")
        print(res)
        
        # Test download
        out_dir = "test_downloads"
        print(f"Downloading to '{out_dir}'...")
        paths = td.download(url, output_dir=out_dir)
        print("Downloaded files:")
        for p in paths:
            print(f" - {p}")
            if not os.path.exists(p):
                print(f"ERROR: {p} does not exist!")
                return False
        return True
    except Exception as e:
        print(f"Error during test: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    test_urls = [
        "https://twitter.com/NASA/status/1678128919655612416", # Has images
        "https://x.com/PythonTrend/status/1802951752935821568" # Has image/video or text
    ]
    
    success = True
    for url in test_urls:
        if not test_download(url):
            success = False
        print("-" * 50)
        
    if success:
        print("ALL TESTS PASSED SUCCESSFULLY!")
        sys.exit(0)
    else:
        print("SOME TESTS FAILED!")
        sys.exit(1)
