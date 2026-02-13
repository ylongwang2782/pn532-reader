# Vault APDU v2 Boundary Test Report

**Date:** 2026-02-13 13:44 CST
**Hardware:** PN7160 (PRO2 v2) ↔ PN532 Reader
**Firmware:** `nfc_test` (commit `0c158c77`, I2C4 400kHz, FWI=4)
**Test Script:** `test_vault_boundary.py`

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

## Performance

| Operation | Data Size | Chunk Size | APDU Rounds | Time | Throughput |
|-----------|-----------|------------|-------------|------|------------|
| Write | 2048B | 32B | 64 | ~15s | ~137 B/s |
| Read | 2048B | 32B | 64 | ~13s | ~158 B/s |

## Bug Fix During Testing

**Issue:** A2 (GET DATA LENGTH) initially failed despite SW=9000.

**Root Cause:** `_exchange_apdu()` ISO-DEP CID detection false positive. The GET LENGTH response `08 00 90 00` has first byte `0x08` which matched the CID I-block PCB pattern `(byte & 0xE8) == 0x08`. The code stripped 2 bytes (PCB + CID), leaving only `90 00` with empty payload.

**Fix:** Added minimum length check (`len(data) >= 5`) before CID stripping to prevent false positives when response payload starts with a byte that coincidentally matches PCB pattern. Also added Le=0x00 to GET DATA LENGTH APDU for proper response framing.

## Notes

- FWI=4 limits single APDU payload to ~32B at I2C 400kHz (FWT=4.8ms)
- 2KB full read/write takes ~30s total due to chunking overhead
- All boundary protection (offset + length > buffer size) works correctly
- Firmware returns SW=6A82 for out-of-bounds access (appropriate "file not found" status)
