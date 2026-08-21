#!/usr/bin/env python3
"""Optional native messaging host that reports the machine's real MAC address.

This exists because a Chrome extension cannot read network hardware itself. If
you install this helper, the extension can ask it for a true hardware id, which
makes the free-trial limit far harder to reset (reinstalling the browser,
clearing storage, or using a different Chrome profile all keep the same MAC).

It is entirely optional: without it the backend falls back to the browser
fingerprint.

--- Install (Linux/macOS) -------------------------------------------------
1. chmod +x trialguard_host.py and put it somewhere permanent, e.g.
   ~/.local/share/trialguard/trialguard_host.py
2. Edit com.trialguard.host.json: set "path" to that absolute path and put your
   real extension id in allowed_origins.
3. Copy the manifest to Chrome's native messaging directory:
     Linux : ~/.config/google-chrome/NativeMessagingHosts/
     macOS : ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/
   Windows: register HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.trialguard.host
            pointing at the manifest file.
4. Add "nativeMessaging" to the extension's permissions.

--- Use from the extension ------------------------------------------------
    const res = await chrome.runtime.sendNativeMessage('com.trialguard.host', {});
    // res.mac_address -> "a4:83:e7:11:22:33"
    // pass it straight into the device fingerprint object
"""
from __future__ import annotations

import json
import struct
import sys
import uuid


def _read_message() -> dict:
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        raise SystemExit(0)
    length = struct.unpack("=I", raw_length)[0]
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8") or "{}")


def _send_message(payload: dict) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def get_mac_address() -> str | None:
    """Primary interface MAC, formatted aa:bb:cc:dd:ee:ff."""
    node = uuid.getnode()
    # getnode() sets the multicast bit when it had to invent a random value.
    if (node >> 40) % 2:
        return None
    return ":".join(f"{(node >> shift) & 0xFF:02x}" for shift in range(40, -8, -8))


def main() -> None:
    try:
        _read_message()
    except (json.JSONDecodeError, struct.error):
        _send_message({"ok": False, "error": "bad request"})
        return

    mac = get_mac_address()
    _send_message(
        {
            "ok": mac is not None,
            "mac_address": mac,
            "platform": sys.platform,
            "error": None if mac else "no stable hardware address available",
        }
    )


if __name__ == "__main__":
    main()
