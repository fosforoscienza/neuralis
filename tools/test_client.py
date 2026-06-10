#!/usr/bin/env python3
"""Client di test per il checkpoint Fase 1.

Si connette al server, verifica di ricevere il broadcast delle feature con tutti
i campi attesi, poi invia un paio di comandi dummy (clear, freeze) che il server
deve loggare. Uso:  python tools/test_client.py [ws://127.0.0.1:8765]
"""
import asyncio
import json
import sys

import websockets

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8765"
REQUIRED = {"type", "warmth", "activation", "iaf", "asymmetry", "powers",
            "signal_quality", "heart_rate", "muse_state", "frozen", "ts"}


async def main():
    print(f"[test] connessione a {URL}")
    async with websockets.connect(URL) as ws:
        await ws.send(json.dumps({"type": "hello", "role": "operator"}))
        got = 0
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "features":
                continue
            got += 1
            if got == 1:
                missing = REQUIRED - set(msg)
                if missing:
                    print(f"[test] FAIL: campi mancanti: {missing}")
                    sys.exit(1)
                print(f"[test] OK schema: muse_state={msg['muse_state']} "
                      f"warmth={msg['warmth']} activation={msg['activation']} "
                      f"iaf={msg['iaf']} quality={msg['signal_quality']} "
                      f"signature={msg['signature']}")
            elif got == 5:
                print(f"[test] feature #5: warmth={msg['warmth']} "
                      f"activation={msg['activation']} frozen={msg['frozen']}")
                print("[test] invio comandi dummy: clear, freeze")
                await ws.send(json.dumps({"cmd": "clear"}))
                await ws.send(json.dumps({"cmd": "freeze"}))
            elif got >= 12:
                print(f"[test] frozen ora = {msg['frozen']} (atteso True dopo freeze)")
                print("[test] PASS: broadcast ricevuto e comandi inviati.")
                return


if __name__ == "__main__":
    asyncio.run(main())
