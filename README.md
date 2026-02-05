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
