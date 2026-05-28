#!/usr/bin/env python3
"""Invia un comando operatore al server e (per 'print') attende l'esito.

Uso:  python tools/send_cmd.py <freeze|print|clear|new_session> [ws-url]
"""
import asyncio
import json
import sys

import websockets

CMD = sys.argv[1] if len(sys.argv) > 1 else "print"
URL = sys.argv[2] if len(sys.argv) > 2 else "ws://127.0.0.1:8765"


async def main():
    async with websockets.connect(URL) as ws:
        await ws.send(json.dumps({"type": "hello", "role": "operator"}))
        await ws.send(json.dumps({"cmd": CMD}))
        print(f"[send] inviato cmd={CMD}")
        if CMD == "print":
            try:
                for _ in range(80):
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                    if m.get("type") == "print_result":
                        print("[send] print_result:", m)
                        return
            except asyncio.TimeoutError:
                print("[send] nessun print_result entro il timeout")


if __name__ == "__main__":
    asyncio.run(main())
