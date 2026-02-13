# Vault APDU v2 Boundary & Stability Test Report

**Date:** 2026-02-13 13:44–14:08 CST
**Hardware:** PN7160 (PRO2 v2) ↔ PN532 Reader
**Firmware:** `nfc_test` (commit `0c158c77`, I2C4 400kHz, FWI=4)
**Test Scripts:** `test_vault_boundary.py`, `test_vault_stability.py`

## Test A: 2KB Full Capacity Read/Write

| # | Test | Result | Detail |
|---|------|--------|--------|
| A1 | Write 2048B (0xAA) | ✅ PASS | 64 APDU rounds × 32B, 15.6s |
| A2 | GET DATA LENGTH | ✅ PASS | Returned 2048 (expected 2048) |
| A3 | Read back 2048B + verify | ✅ PASS | All bytes 0xAA confirmed, 13.4s |
| A4 | Write 2048B incrementing (0x00–0xFF cycle) | ✅ PASS | 13.9s |
| A5 | Spot-check offsets 0 / 1024 / 2016 | ✅ PASS | 32B match at each offset |
| A6 | Out-of-bounds write (offset=2040, 16B) | ✅ PASS | Firmware rejected with SW=6A82 |
| A7 | Out-of-bounds read (offset=2040, 16B) | ✅ PASS | Firmware rejected |
| A8 | Extreme OOB write (offset=2048, 1B) | ✅ PASS | Firmware rejected with SW=6A82 |

**Result: 8/8 PASSED ✅**

## Test C: Continuous Stability (10 rounds)

Each round: write 2048B random pattern → read back → byte-level verify.

| Round | Seed | Write | Read | Total | Result |
|-------|------|-------|------|-------|--------|
| C01 | 0x67 | 15.9s | 14.4s | 30.2s | ✅ |
| C02 | 0x9C | 13.7s | 14.2s | 27.9s | ✅ |
| C03 | 0x69 | 14.3s | 13.9s | 28.3s | ✅ |
| C04 | 0xA5 | 13.9s | 13.7s | 27.6s | ✅ |
| C05 | 0x95 | 13.9s | 13.6s | 27.5s | ✅ |
| C06 | 0xAB | 13.8s | 13.0s | 26.8s | ✅ |
| C07 | 0x82 | 13.2s | 13.0s | 26.2s | ✅ |
| C08 | 0x60 | 13.8s | 13.5s | 27.3s | ✅ |
| C09 | 0xBD | 13.5s | 13.8s | 27.3s | ✅ |
| C10 | 0xE4 | 13.6s | 13.4s | 27.0s | ✅ |

**Result: 10/10 PASSED ✅** (total 276s, avg 27.6s/round)

Final GET DATA LENGTH: 2048 ✅

## Performance Summary

| Operation | Data Size | Chunk Size | APDU Rounds | Avg Time | Throughput |
|-----------|-----------|------------|-------------|----------|------------|
| Write | 2048B | 32B | 64 | ~14s | ~146 B/s |
| Read | 2048B | 32B | 64 | ~13.5s | ~152 B/s |
| Full cycle | 2048B | 32B | 128 | ~27.5s | ~149 B/s |

## Bugs Found & Fixed

### Bug 1: GET DATA LENGTH empty payload (A2)

**Symptom:** A2 returned `success=false` despite SW=9000.

**Root Cause:** GET DATA LENGTH APDU `80 CA 00 00` was missing the Le byte. Without Le, the card returns no response data. Additionally, the response `08 00` (length=2048) had its first byte `0x08` misidentified as an ISO-DEP I-block PCB header by the CID detection heuristic.

**Fix:** Added Le=0x00 to the APDU (`80 CA 00 00 00`).

### Bug 2: CID detection false positive causing +2 byte offset (Test C)

**Symptom:** 50% of stability test rounds failed with consistent +2 byte offset errors. All mismatched bytes showed `expected_value + 2`.

**Root Cause:** `_exchange_apdu()` had a workaround for PN532-to-PN532 ISO-DEP CID frame leaks. The heuristic `(data[0] & 0xE8) == 0x08` matches bytes `0x08–0x0F` and `0x18–0x1F` (16/256 = 6.25% collision probability per chunk). When a 32B read chunk's first payload byte happened to match this pattern, the code incorrectly stripped 2 bytes (PCB + CID), shifting all subsequent bytes by +2.

**Analysis:**
- PN7160 ATS = `78 80 40 00`, TC1 = `0x40` → CID bit (bit 1) = **0** → CID not supported
- The workaround was only needed for PN532-to-PN532 scenarios, not PN7160
- With 64 read chunks per 2KB, ~4 chunks per round would collide (6.25% × 64 ≈ 4)

**Verification:** All 5 failing rounds confirmed — first error byte in each matched the CID pattern:

| Round | Seed | Error Offset | First Byte | CID Match |
|-------|------|-------------|------------|-----------|
| C02 | 0x4D | 192 | 0x0D | ✅ |
| C03 | 0x3C | 224 | 0x1C | ✅ |
| C05 | 0x6B | 160 | 0x0B | ✅ |
| C09 | 0x08 | 0 | 0x08 | ✅ |
| C10 | 0xEB | 32 | 0x0B | ✅ |

**Fix:** Changed CID stripping from always-on to opt-in (`strip_cid=False` by default). Only enable for known PN532-to-PN532 scenarios where raw ISO-DEP I-blocks actually leak through.

## Notes

- FWI=4 limits single APDU payload to ~32B at I2C 400kHz (FWT=4.8ms)
- 2KB full read/write takes ~27.5s per cycle due to chunking overhead
- All boundary protection (offset + length > buffer size) works correctly
- Firmware returns SW=6A82 for out-of-bounds access
- PN7160 stable across 10 consecutive full-buffer cycles (20KB written, 20KB read, zero errors)
