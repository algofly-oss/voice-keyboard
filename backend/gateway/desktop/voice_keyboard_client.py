#!/usr/bin/env python3
"""Small cross-platform desktop client for the Voice Keyboard gateway."""

import argparse
import asyncio
import json
import os
import platform
import sys
from pathlib import Path

import websockets
from pynput.keyboard import Controller, Key

APP_DIR = Path(os.environ.get("APPDATA", Path.home() / ".config")) / "voice-keyboard"
CONFIG = APP_DIR / "config.json"


def log(message):
    print(f"[voice-keyboard] {message}", flush=True)


def load_config():
    if not CONFIG.exists():
        raise SystemExit("Not enrolled yet. Run the install command from the Voice Keyboard web UI.")
    return json.loads(CONFIG.read_text())


def save_config(config):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    try:
        CONFIG.chmod(0o600)
    except OSError:
        pass


def keyboard_controller():
    keyboard = Controller()
    if sys.platform == "darwin":
        try:
            from Quartz import AXIsProcessTrusted
            if not AXIsProcessTrusted():
                log("macOS Accessibility permission is not active for this Python process")
        except ImportError:
            log("Install pyobjc-framework-Quartz for the macOS permission diagnostic")
    return keyboard


def ws_url(server):
    return server.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/v1/keyboard"


async def enroll(args):
    client_id = args.client or platform.node() or "desktop"
    url = ws_url(args.server) + "?token=" + args.token
    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as socket:
        await socket.send(json.dumps({"type": "hello", "client": client_id, "room": args.room}))
        ready = json.loads(await socket.recv())
    save_config({"server": args.server, "room": args.room, "client": client_id,
                 "credential": ready["credential"]})
    log(f"Enrolled as {client_id}. Configuration saved to {CONFIG}")


async def run():
    config = load_config()
    keyboard = keyboard_controller()
    url = ws_url(config["server"]) + "?token=" + config["credential"]
    log(f"Connecting as {config['client']} to {config['server']} (room: {config['room']})")
    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as socket:
        await socket.send(json.dumps({"type": "hello", "client": config["client"], "room": config["room"]}))
        ready = json.loads(await socket.recv())
        log(f"Connected and ready; server room: {ready.get('room')}")
        received = 0
        async for raw in socket:
            message = json.loads(raw)
            if message.get("type") == "segment":
                received += 1
                text = message.get("text", "")
                log(f"Received segment {received}: {text!r}")
                try:
                    keyboard.type(text)
                    log(f"Typed segment {received}")
                except Exception as error:
                    log(f"Keyboard input failed: {error}. Check macOS Accessibility permissions for this terminal/Python.")
                    raise
            elif message.get("type") == "key":
                send_key(keyboard, message["key"], message.get("state", "press"))


def type_test(text):
    keyboard = keyboard_controller()
    log("Type test starts in 3 seconds; focus the target application now")
    import time
    time.sleep(3)
    keyboard.type(text)
    log(f"Type test sent: {text!r}")


def send_key(keyboard, name, state):
    keys = {"backspace": Key.backspace, "up": Key.up, "down": Key.down,
            "left": Key.left, "right": Key.right, "enter": Key.enter}
    key = keys[name]
    if state == "up":
        keyboard.release(key)
    else:
        keyboard.press(key)
        if state == "press":
            keyboard.release(key)


def main():
    parser = argparse.ArgumentParser(description="Voice Keyboard desktop client")
    sub = parser.add_subparsers(dest="command", required=True)
    enroll_parser = sub.add_parser("enroll", help="enroll this computer")
    enroll_parser.add_argument("--server", required=True)
    enroll_parser.add_argument("--token", required=True)
    enroll_parser.add_argument("--room", default="voice-keyboard")
    enroll_parser.add_argument("--client", default="")
    sub.add_parser("start", help="run the keyboard client")
    test_parser = sub.add_parser("type-test", help="type a local test string")
    test_parser.add_argument("text", nargs="?", default="Voice Keyboard test")
    args = parser.parse_args()
    if args.command == "enroll":
        asyncio.run(enroll(args))
    elif args.command == "type-test":
        type_test(args.text)
    else:
        while True:
            try:
                asyncio.run(run())
            except KeyboardInterrupt:
                return
            except Exception as error:
                log(f"Connection lost: {error}; retrying in 5 seconds")
                try:
                    asyncio.run(asyncio.sleep(5))
                except KeyboardInterrupt:
                    return


if __name__ == "__main__":
    main()
