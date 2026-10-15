#!/usr/bin/env python3
"""
Comprehensive test script for V2.0 TeraBox resolver (Direct Method A & Account Method B).
Designed for running locally or on VPS.

Usage:
    python test_terabox_v2_vps.py [url] [cookie]
"""

import asyncio
import logging
import sys
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)
log = logging.getLogger("TeraBoxVPSTest")

from downloader.sites import terabox
from downloader import fetcher


async def test_resolver(share_url: str, cookie: str):
    log.info("=" * 70)
    log.info("V2.0 TERABOX RESOLVER & DOWNLOAD TEST (METHODS A & B)")
    log.info("=" * 70)
    log.info(f"Target URL: {share_url}")
    log.info(f"Cookie provided: {cookie[:20]}...")
    log.info("")

    output_dir = Path("./downloads")
    output_dir.mkdir(parents=True, exist_ok=True)

    log.info("Calling terabox.attempt() with cookie...")
    try:
        resolution = await terabox.attempt(
            share_url,
            cookies=cookie
        )

        log.info("=" * 70)
        log.info("RESOLUTION SUCCESSFUL!")
        log.info(f"  Engine: {resolution.engine}")
        log.info(f"  Source URL: {resolution.source_url}")
        log.info(f"  Total Files Found: {len(resolution.files)}")
        log.info("=" * 70)
        log.info("")

        for idx, file_item in enumerate(resolution.files, 1):
            size_mb = (file_item.filesize or 0) / (1024 * 1024)
            log.info(f"  [{idx}] {file_item.filename}")
            log.info(f"      Size: {file_item.filesize} bytes ({size_mb:.2f} MB)")
            log.info(f"      Transfers: {len(file_item.transfers)}")
            for t in file_item.transfers:
                log.info(f"          - URL: {t.url[:80]}...")
            log.info("")

        # Select lowest size file
        sorted_files = sorted(
            resolution.files,
            key=lambda f: f.filesize if f.filesize is not None and f.filesize > 0 else float('inf')
        )
        lowest_file = sorted_files[0]

        log.info("=" * 70)
        log.info("LOWEST SIZE FILE SELECTED FOR DOWNLOAD:")
        log.info(f"  Name: {lowest_file.filename}")
        log.info(f"  Size: {lowest_file.filesize} bytes")
        log.info("=" * 70)
        log.info("")

        log.info("Starting download via fetcher.fetch()...")

        def progress(p):
            log.info(f"Progress: {p.transfer_done}/{p.transfer_total} bytes (overall: {p.overall_done}/{p.overall_total})")

        destination_path = output_dir / lowest_file.filename
        saved_file = await fetcher.fetch(
            lowest_file,
            destination=destination_path,
            progress=progress
        )

        log.info("")
        log.info("=" * 70)
        log.info(f"SUCCESS: File downloaded and saved to: {saved_file.path}")
        log.info("=" * 70)
        return 0

    except Exception as exc:
        log.error(f"Resolution or download failed: {exc}")
        import traceback
        log.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.1024tera.com/wap/share/filelist?surl=Roh2t3fSKC3Ga-tHyVclqQ"
    ndus = sys.argv[2] if len(sys.argv) > 2 else "ndus=Y2vX0LNpeHuiLRsD5H0uuF8aIlPy90FB3D07sSSi"
    sys.exit(asyncio.run(test_resolver(url, ndus)))
