#!/usr/bin/env python3
"""
PN532 dev board: Vault card emulation with test data.
PRO2 (PN7160 reader mode) should be able to poll, SELECT, and READ this data.

Usage: python3 test_emu_for_pro2.py
Press Ctrl+C to stop.
"""

import sys
import os
import threading
import time
import signal
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pn532 import PN532, VaultTagEmulator

BAUD_RATE = 115200

def find_serial_port():
    """Auto-detect PN532 USB serial port."""
    import glob
    ports = glob.glob("/dev/tty.usbserial-*")
    if len(ports) == 1:
        return ports[0]
    elif len(ports) > 1:
        print(f"Multiple serial ports found: {ports}")
        print(f"Using first: {ports[0]}")
        return ports[0]
    else:
        print("No /dev/tty.usbserial-* found!")
        sys.exit(1)

SERIAL_PORT = find_serial_port()

def make_test_data(size=2048):
    """Generate recognizable test pattern: 0x00..0xFF repeating."""
    return bytes([i & 0xFF for i in range(size)])

def main():
    signal.alarm(0)  # clear any inherited alarm

    test_data = make_test_data(2048)
    print(f"Test data: {len(test_data)} bytes (0x00..0xFF repeating)")
    print(f"First 16 bytes: {test_data[:16].hex()}")
    print()

    emulator = VaultTagEmulator(initial_data=test_data)
    print(f"VaultTagEmulator ready, buffer={emulator.BUFFER_SIZE} bytes")
    print(f"Vault AID: {emulator.VAULT_AID.hex()}")
    print()

    pn532 = PN532(SERIAL_PORT, baudrate=BAUD_RATE)
    stop_event = threading.Event()
    logs = deque(maxlen=500)

    print(f"Starting card emulation on {SERIAL_PORT}...")
    print("Waiting for PRO2 reader to connect...")
    print("Press Ctrl+C to stop.\n")
    sys.stdout.flush()

    # Run emulation in a thread so we can print logs
    def run_emu():
        try:
            pn532.emulate_tag(emulator, stop_event, logs)
        except Exception as e:
            print(f"Emulation error: {e}")

    emu_thread = threading.Thread(target=run_emu, daemon=True)
    emu_thread.start()

    try:
        last_log_count = 0
        while emu_thread.is_alive():
            current_count = len(logs)
            if current_count > last_log_count:
                for i in range(last_log_count, min(current_count, last_log_count + 50)):
                    try:
                        entry = logs[i]
                        d = entry.get("direction", "?")
                        data = entry.get("data", "")
                        if d == "ERR":
                            print(f"  ❌ {data}")
                        elif "8c" in str(data).lower() or "8d" in str(data).lower() or "86" in str(data).lower() or "87" in str(data).lower():
                            # TgInitAsTarget(8C), TgResponseToInitiator(8D), TgGetData(86), TgSetData(8E)
                            short = data[:60] + "..." if len(str(data)) > 60 else data
                            print(f"  [{d}] {short}")
                    except IndexError:
                        pass
                last_log_count = current_count
            time.sleep(0.2)
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nStopping...")
        stop_event.set()
        emu_thread.join(timeout=5)

    print("Done.")

if __name__ == "__main__":
    main()
