#!/usr/bin/env python3
"""
Telegram Upload Stress / Failure Mode Simulator (A3)
"""
import argparse, asyncio, os, sys, time, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "live"))

os.environ.setdefault("SECRET_KEY", "stress-test")
os.environ.setdefault("APP_ENV", "test")

from streamly.app import create_app
from streamly.routes.telegram_client import manager as tg_manager

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--uploads", type=int, default=2)
    p.add_argument("--simulate-disconnect", action="store_true")
    p.add_argument("--duration", type=int, default=4)
    args = p.parse_args()

    app = create_app()
    with app.app_context():
        print("=== TELEGRAM STRESS (A3) ===")
        print("Before:", tg_manager.stats_dict())

        clients = []
        for i in range(args.uploads):
            try:
                c = tg_manager.create_client(f"stress-{i}-{uuid.uuid4().hex[:4]}")
                clients.append(c)
                print(f"  created client {i+1}")
            except Exception as e:
                print(f"  client {i} err (expected): {e}")

        async def run():
            for i, c in enumerate(clients):
                try:
                    await tg_manager.safe_connect(c)
                    print(f"  connected {i+1}")
                    if args.simulate_disconnect and i == 0:
                        await asyncio.sleep(0.8)
                        await tg_manager.safe_disconnect(c)
                        print(f"  forced disconnect on {i+1}")
                    await asyncio.sleep(0.3)
                except Exception as e:
                    print(f"  err on {i}: {e}")
            await asyncio.sleep(args.duration)
            for c in clients:
                await tg_manager.safe_disconnect(c)

        asyncio.run(run())
        print("After:", tg_manager.stats_dict())
        print("Stress complete (simulated).")

if __name__ == "__main__":
    main()
