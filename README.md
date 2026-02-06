# PN532 NFC Reader

Web interface for PN532 NFC reader using libnfc.

## Requirements

- Python 3.10+
- libnfc (`brew install libnfc`)
- PN532 board connected via USB-UART

## Usage

```bash
# Start server
./start.sh

# Stop server
./stop.sh
```

Open http://localhost:5001 in browser.

## Features

- Scan NFC cards (ISO14443A)
- Display UID, ATQA, SAK, ATS
- Auto-scan on page load
- Communication log with parsed PN532 frame display
- CSV export for communication logs
- Type 4 tag emulation with NDEF content

## PN532 Frame Format

All communication with the PN532 uses the Normal Information Frame format:

| Field | Bytes | Description |
|-------|-------|-------------|
| Preamble | `00` | Sync byte |
| Start Code | `00 FF` | Frame start marker |
| LEN | 1 byte | Data length (TFI + DATA) |
| LCS | 1 byte | Length checksum (LEN + LCS = 0x00 mod 256) |
| TFI | `D4` or `D5` | `D4` = Host→PN532, `D5` = PN532→Host |
| DATA | LEN-1 bytes | Command code + parameters |
| DCS | 1 byte | Data checksum (TFI + DATA + DCS = 0x00 mod 256) |
| Postamble | `00` | Frame end |

### Special Frames

- **ACK**: `00 00 FF 00 FF 00` — Acknowledgement
- **NACK**: `00 00 FF FF 00 00` — Negative acknowledgement
- **Wakeup Preamble**: Series of `55` bytes sent to wake the PN532 from sleep

### Common Command Codes

Commands are sent with TFI=`D4`. Responses use TFI=`D5` with response code = command code + 1.

| Code | Command | Description |
|------|---------|-------------|
| `0x00` | Diagnose | Self-diagnostic test |
| `0x02` | GetFirmwareVersion | Read firmware version |
| `0x06` | ReadRegister | Read register value |
| `0x08` | WriteRegister | Write register value |
| `0x12` | SetParameters | Set internal parameters |
| `0x14` | SAMConfiguration | Configure SAM (Security Access Module) |
| `0x16` | PowerDown | Enter power-down mode |
| `0x32` | RFConfiguration | Configure RF communication |
| `0x42` | InCommunicateThru | Transparent data exchange with target |
| `0x44` | InRelease | Release target |
| `0x4A` | InListPassiveTarget | Detect and list passive targets |
| `0x52` | InDeselect | Deselect target |

## Example: NFC Scan Communication Analysis

A complete libnfc-driven PN532 NFC card scan flow (348 lines of communication records).

### 1. Initialization Phase

| Step | Command | Description |
|------|---------|-------------|
| Wakeup | `55 55 00 ...` | Preamble to wake up PN532 |
| SAMConfiguration (`0x14`) | Parameter: `01` (Normal mode) | No SAM, direct mode |
| Diagnose (`0x00`) | Sends `libnfc` string | Communication loopback test, PN532 echoes back `libnfc` confirming normal communication |
| GetFirmwareVersion (`0x02`) | Returns `32 01 06 07` | **PN532, v1.6**, supports ISO14443A/B + ISO18092 |
| SetParameters (`0x12`) | Parameter: `0x14` | Enable automatic ATR_RES and RATS |

### 2. RF Field Configuration and Register Setup

- **ReadRegister / WriteRegister**: Configure CIU analog front-end registers (TxMode, RxMode, TxControl, etc.)
- **RFConfiguration (item 0x01)**: RF field off (`0x00`) then on (`0x01`), resetting the RF field
- **RFConfiguration (item 0x05)**: Set MaxRetries = `FF FF FF` (infinite retries), preparing for polling
- Reconfigure registers `0x6305=0x40`, `0x633C=0x10` to adjust analog front-end parameters
- Reset MaxRetries = `00 01 02` (fast polling mode)

### 3. Multi-Protocol Card Polling

#### ISO 14443-4 Type A (106 kbps) — Card Found

**InListPassiveTarget** (`0x4A`, BrTy=`0x00`) — successfully found 1 card:

| Field | Value | Meaning |
|-------|-------|---------|
| **ATQA** | `00 02` | ISO 14443A response |
| **SAK** | `0x20` | **ISO 14443-4 compatible** (supports ISO-DEP) |
| **UID** | `7B 9A DC E3` | 4-byte UID |
| **ATS** | `05 78 80 B8 02` | See breakdown below |

**ATS Breakdown:**
- `TL=05`: ATS length 5 bytes
- `T0=78`: FSCI=8 (max frame 256 bytes), TA1/TB1/TC1 all present
- `TA1=80`: Only supports 106 kbps bidirectional
- `TB1=B8`: FWI=11 (frame waiting time ~620ms), SFGI=8 (~77ms)
- `TC1=02`: Supports CID, does not support NAD

**Card Type Inference**: SAK=`0x20` + 4-byte random UID + above ATS parameters, highly consistent with contactless smart card characteristics (such as EMV contactless payment card, MIFARE Plus SL3, or JCOP card).

#### Release Then Continue Polling Other Protocols

Card was released via **InRelease** (`0x44`), then tried sequentially:

| Protocol | BrTy | Result |
|----------|------|--------|
| ISO 14443A 106kbps (retry) | `0x00` | Not found |
| FeliCa 212 kbps | `0x01` | Not found |
| FeliCa 424 kbps | `0x02` | Not found |
| ISO 14443B 106kbps | `0x03` | Not found |

### 4. Low-Level Protocol Probing

libnfc uses direct CIU register manipulation + **InCommunicateThru** (`0x42`) for lower-level protocol probing:

- **Group 1** (registers = `0x83 0x83`, ISO14443B frame format):
  - Send `01 0b 3f 80` — Timeout
  - Send `06 00` — Timeout
  - Send `10` — Timeout

- **Group 2** (reconfigure 10 CIU registers for other protocols, possibly ISO 15693/NFC-V):
  - Send `0a` — Timeout
  - Send `0c` — Timeout

All timed out, indicating no cards of other protocol types in the field.

### 5. Final Polling and Cleanup

| Step | Command | Result |
|------|---------|--------|
| InListPassiveTarget (Jewel/Topaz, BrTy=`0x04`) | `0x4A` | Not found |
| RFConfiguration (RF field off) | `0x32 01 00` | RF field off |
| ReadRegister → InCommunicateThru (empty) | Empty data | Success (cleanup) |
| InRelease | `0x52 00` | Release all targets |
| RFConfiguration (RF field off) | `0x32 01 00` | RF field off again |
| **PowerDown** (`0x16`) | WakeUp=`0xF0` | PN532 enters low-power sleep |

### Scan Summary

| Item | Information |
|------|-------------|
| **Reader** | PN532 v1.6 (libnfc driver) |
| **Scan Protocols** | ISO14443A, ISO14443B, FeliCa 212/424, Jewel/Topaz, low-level protocol probing |
| **Card Found** | 1 ISO 14443-4 Type A card |
| **UID** | `7B:9A:DC:E3` |
| **SAK** | `0x20` (ISO 14443-4 compatible, not MIFARE Classic) |
| **Max Frame Length** | 256 bytes |
| **Communication Speed** | 106 kbps |
| **Likely Card Type** | EMV contactless payment card / MIFARE Plus SL3 / JCOP smart card |
