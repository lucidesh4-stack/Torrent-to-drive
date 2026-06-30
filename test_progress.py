"""
Test Progress Bar (No Telegram needed)
=======================================

This tests just the progress bar visualization.
Run this to see the progress bar style before using with Telegram.
"""

import time
import random

# Import progress bar from main module
import sys
sys.path.insert(0, '.')

from telegram_uploader import ProgressBar, print_progress


def simulate_progress(total_bytes: int, speed_mbps: float, label: str = "Test"):
    """
    Simulate a progress bar with random speed variations.
    
    Args:
        total_bytes: Total size in bytes
        speed_mbps: Target speed in Mbps
        label: Label for the progress bar
    """
    progress = ProgressBar(total_bytes, prefix=label, bar_length=40)
    
    # Calculate bytes per second from Mbps
    bytes_per_sec = speed_mbps * 1_000_000 / 8
    
    current = 0
    while current < total_bytes:
        # Add some randomness to simulate real speed variations
        variation = random.uniform(0.7, 1.3)
        chunk = int(bytes_per_sec * 0.1 * variation)  # 100ms chunks
        current = min(current + chunk, total_bytes)
        
        progress.update(current)
        print_progress(progress)
        time.sleep(0.1)
    
    print()  # New line after complete


def main():
    print("=" * 70)
    print("PROGRESS BAR TEST")
    print("=" * 70)
    print()
    print("Simulating downloads and uploads with different speeds...")
    print()
    
    # Test 1: Fast download (100 Mbps)
    print("\n[TEST 1] Fast Download - 100 Mbps")
    print("-" * 50)
    simulate_progress(100 * 1024 * 1024, 100, "DL")
    
    # Test 2: Medium upload (50 Mbps)
    print("\n[TEST 2] Medium Upload - 50 Mbps")
    print("-" * 50)
    simulate_progress(50 * 1024 * 1024, 50, "UL")
    
    # Test 3: Slow download (10 Mbps)
    print("\n[TEST 3] Slow Download - 10 Mbps")
    print("-" * 50)
    simulate_progress(10 * 1024 * 1024, 10, "DL")
    
    # Test 4: Very fast (200 Mbps)
    print("\n[TEST 4] Very Fast - 200 Mbps")
    print("-" * 50)
    simulate_progress(200 * 1024 * 1024, 200, "FAST")
    
    print("\n" + "=" * 70)
    print("PROGRESS BAR TEST COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()