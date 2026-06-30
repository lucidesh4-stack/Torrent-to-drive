"""
Speed test for optimized downloader.
Tests Worker vs Direct streaming.
"""

import asyncio
import time
import httpx

WORKER_URL = "https://streamly-proxy.lucidesh.workers.dev/"
SEEDR_URL = "https://rd11.seedr.cc/ff_get/1426659/5946465006/House.of.the.Dragon.S03E02.1080p.HEVC.x265-MeGusta[EZTVx.to].mkv?st=_o1_4YAOJ7992pKYCCGblw&e=1782914162"

TEST_SIZE_MB = 50


async def test_worker():
    """Test Worker proxy speed."""
    import urllib.parse
    
    print(f"\n{'='*60}")
    print(f"Testing: Worker Proxy ({TEST_SIZE_MB}MB)")
    print(f"{'='*60}")
    
    encoded_url = urllib.parse.quote(SEEDR_URL, safe='')
    worker_endpoint = f"{WORKER_URL}?url={encoded_url}"
    
    start = time.time()
    bytes_recv = 0
    
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("GET", worker_endpoint) as r:
            print(f"Status: {r.status_code}")
            
            if r.status_code == 403:
                print("⚠️ Worker blocked (403)")
                return None
            
            async for chunk in r.aiter_bytes(chunk_size=512*1024):
                bytes_recv += len(chunk)
                
                pct = (bytes_recv / (TEST_SIZE_MB * 1024 * 1024)) * 100
                if pct % 20 < 1:
                    elapsed = time.time() - start
                    speed = (bytes_recv / 1024 / 1024 / elapsed) * 8
                    print(f"  {pct:.0f}% - {speed:.1f} Mbps", end="\r")
                
                if bytes_recv >= TEST_SIZE_MB * 1024 * 1024:
                    break
    
    elapsed = time.time() - start
    speed_mbps = (bytes_recv / 1024 / 1024 / elapsed) * 8
    
    print(f"\n  Complete: {bytes_recv // 1024 // 1024}MB in {elapsed:.1f}s")
    print(f"  Speed: {speed_mbps:.2f} Mbps")
    
    return speed_mbps


async def test_direct():
    """Test Direct stream speed."""
    print(f"\n{'='*60}")
    print(f"Testing: Direct Stream ({TEST_SIZE_MB}MB)")
    print(f"{'='*60}")
    
    start = time.time()
    bytes_recv = 0
    
    async with httpx.AsyncClient(timeout=300.0, http2=True) as client:
        async with client.stream("GET", SEEDR_URL) as r:
            print(f"Status: {r.status_code}")
            
            async for chunk in r.aiter_bytes(chunk_size=512*1024):
                bytes_recv += len(chunk)
                
                pct = (bytes_recv / (TEST_SIZE_MB * 1024 * 1024)) * 100
                if pct % 20 < 1:
                    elapsed = time.time() - start
                    speed = (bytes_recv / 1024 / 1024 / elapsed) * 8
                    print(f"  {pct:.0f}% - {speed:.1f} Mbps", end="\r")
                
                if bytes_recv >= TEST_SIZE_MB * 1024 * 1024:
                    break
    
    elapsed = time.time() - start
    speed_mbps = (bytes_recv / 1024 / 1024 / elapsed) * 8
    
    print(f"\n  Complete: {bytes_recv // 1024 // 1024}MB in {elapsed:.1f}s")
    print(f"  Speed: {speed_mbps:.2f} Mbps")
    
    return speed_mbps


async def main():
    print("=" * 60)
    print("SPEED TEST - Worker vs Direct")
    print("=" * 60)
    print(f"Test URL: {SEEDR_URL[:60]}...")
    print(f"Test size: {TEST_SIZE_MB}MB")
    
    worker_speed = await test_worker()
    await asyncio.sleep(2)
    
    direct_speed = await test_direct()
    
    print(f"\n{'='*60}")
    print("RESULTS")
    print(f"{'='*60}")
    
    if worker_speed:
        print(f"Worker:  {worker_speed:.2f} Mbps")
    if direct_speed:
        print(f"Direct:  {direct_speed:.2f} Mbps")
    
    if worker_speed and direct_speed:
        ratio = worker_speed / direct_speed
        print(f"\nRatio: {ratio:.1f}x")
        
        if ratio > 1.2:
            print("→ Worker is faster")
        elif ratio < 0.8:
            print("→ Direct is faster")
        else:
            print("→ Similar performance")


if __name__ == "__main__":
    asyncio.run(main())