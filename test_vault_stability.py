#!/usr/bin/env python3
"""Vault APDU v2 — Test C: Continuous Stability."""
import sys
import time
import os
import random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pn532 import PN532

BUF_SIZE = 2048
CHUNK = 32
ROUNDS = 10  # number of full write/read/verify cycles

def fmt_time(t):
    return f"{t:.1f}s"

def test_c():
    pn = PN532()
    pn._ensure_open()
    pn.sam_configuration()
    pn._close()
    time.sleep(0.5)

    print("=" * 64)
    print("  Test C: Continuous Stability")
    print("=" * 64)
    print(f"\n  Rounds: {ROUNDS} full write/read/verify cycles")
    print(f"  Buffer: {BUF_SIZE}B, chunk: {CHUNK}B")
    print(f"  Each round: write random pattern → read back → verify\n")

    passed = 0
    failed = 0
    total_time = 0

    for i in range(1, ROUNDS + 1):
        # Generate random data for this round
        seed = random.randint(0, 255)
        pattern = bytes([(seed + j) % 256 for j in range(BUF_SIZE)])

        print(f"[C{i:02d}] Round {i}/{ROUNDS} (seed=0x{seed:02X})")

        # Write
        t0 = time.time()
        r = pn.write_vault_tag(0, pattern)
        write_time = time.time() - t0

        if not (r and r.get('success')):
            err = r.get('error', 'unknown') if r else 'no response'
            print(f"  ❌ 写入失败: {err}")
            failed += 1
            time.sleep(1)
            continue

        time.sleep(0.3)

        # Read back
        t0 = time.time()
        r = pn.read_vault_tag(0, BUF_SIZE)
        read_time = time.time() - t0

        if not (r and r.get('success')):
            err = r.get('error', 'unknown') if r else 'no response'
            print(f"  ❌ 读取失败: {err}")
            failed += 1
            time.sleep(1)
            continue

        # Extract and verify
        raw = r.get('data_hex', '')
        if raw:
            data = bytes.fromhex(raw.replace(' ', ''))
        else:
            data = b''

        round_time = write_time + read_time
        total_time += round_time

        if len(data) != BUF_SIZE:
            print(f"  ❌ 长度不匹配: got {len(data)}B, expected {BUF_SIZE}B ({fmt_time(round_time)})")
            failed += 1
            continue

        mismatches = sum(1 for j in range(BUF_SIZE) if data[j] != pattern[j])
        if mismatches == 0:
            print(f"  ✅ 校验通过 (W:{fmt_time(write_time)} R:{fmt_time(read_time)} = {fmt_time(round_time)})")
            passed += 1
        else:
            first_bad = next(j for j in range(BUF_SIZE) if data[j] != pattern[j])
            print(f"  ❌ {mismatches} 字节不匹配, 首个 @ offset {first_bad}: "
                  f"expected 0x{pattern[first_bad]:02X}, got 0x{data[first_bad]:02X} ({fmt_time(round_time)})")
            failed += 1

        time.sleep(0.3)

    # GET LENGTH after all rounds
    print(f"\n[C-LEN] 最终 GET DATA LENGTH")
    r = pn.get_vault_length()
    if r and r.get('success') and r.get('length') == BUF_SIZE:
        print(f"  ✅ Length = {r['length']}")
    else:
        err = r.get('error', 'unknown') if r else 'no response'
        print(f"  ⚠️  GET LENGTH: {err}")

    # Summary
    print("\n" + "=" * 64)
    status = "ALL PASSED ✅" if failed == 0 else f"{failed} FAILED ❌"
    avg = total_time / max(passed, 1)
    print(f"  Test C 结果: {passed}/{ROUNDS} passed — {status}")
    print(f"  总耗时: {fmt_time(total_time)}, 平均每轮: {fmt_time(avg)}")
    print("=" * 64)
    return failed == 0

if __name__ == "__main__":
    success = test_c()
    sys.exit(0 if success else 1)
