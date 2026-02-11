"""
PN532 direct serial communication for ISO 14443A card scanning and
Type 4 Tag emulation.
"""

import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import serial


# PN532 frame constants
PREAMBLE = 0x00
START1 = 0x00
START2 = 0xFF
POSTAMBLE = 0x00
TFI_HOST_TO_PN532 = 0xD4
TFI_PN532_TO_HOST = 0xD5

ACK_FRAME = bytes([0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00])

BAUDRATE = 115200


def _find_serial_port():
    """Auto-detect the USB-serial port for the PN532."""
    import glob
    candidates = glob.glob("/dev/tty.usbserial-*")
    if candidates:
        return candidates[0]
    return "/dev/tty.usbserial-1410"  # fallback


SERIAL_PORT = _find_serial_port()


@dataclass
class CardInfo:
    """Parsed ISO 14443A card information."""
    uid: str = ""
    atqa: str = ""
    sak: str = ""
    ats: str = ""
    device: str = ""
    type: str = "ISO/IEC 14443A (106 kbps) target"


class PN532:
    """PN532 NFC reader via direct UART communication."""

    @staticmethod
    def list_ports():
        """Return all available /dev/tty.usbserial-* ports."""
        import glob
        return sorted(glob.glob("/dev/tty.usbserial-*"))

    def __init__(self, port=SERIAL_PORT, baudrate=BAUDRATE):
        self._port = port
        self._baudrate = baudrate
        self._serial = None
        self._lock = threading.Lock()

    def set_port(self, port):
        """Close existing connection and switch to a new serial port."""
        with self._lock:
            self._close()
            self._port = port

    def _build_frame(self, cmd, params=b""):
        """Build a PN532 normal information frame."""
        data = bytes([TFI_HOST_TO_PN532, cmd]) + bytes(params)
        length = len(data)
        lcs = (0x100 - length) & 0xFF
        dcs = (0x100 - sum(data)) & 0xFF
        return bytes([PREAMBLE, START1, START2, length, lcs]) + data + bytes([dcs, POSTAMBLE])

    def _format_hex(self, data):
        """Format bytes as space-separated hex string."""
        return " ".join(f"{b:02x}" for b in data)

    def _timestamp(self):
        """Get current timestamp string."""
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def _send_command(self, cmd, params=b"", timeout=1.0, logs=None):
        """
        Send a command frame, wait for ACK, then read the response frame.
        Returns the response data (after TFI) or None on failure.
        """
        frame = self._build_frame(cmd, params)

        # Log TX
        if logs is not None:
            logs.append({
                "time": self._timestamp(),
                "direction": "TX",
                "data": self._format_hex(frame),
            })

        self._serial.write(frame)
        self._serial.flush()

        # Read ACK (6 bytes)
        ack = self._serial.read(6)
        if ack != ACK_FRAME:
            if logs is not None:
                logs.append({
                    "time": self._timestamp(),
                    "direction": "RX",
                    "data": self._format_hex(ack) if ack else "(no ACK)",
                })
            return None

        # Log ACK
        if logs is not None:
            logs.append({
                "time": self._timestamp(),
                "direction": "RX",
                "data": self._format_hex(ack),
            })

        # Read response frame header: PREAMBLE(1) START(2) LEN(1) LCS(1) = 5 bytes
        deadline = time.monotonic() + timeout
        header = b""
        while len(header) < 5 and time.monotonic() < deadline:
            remaining = 5 - len(header)
            self._serial.timeout = max(0.01, deadline - time.monotonic())
            chunk = self._serial.read(remaining)
            if chunk:
                header += chunk

        if len(header) < 5:
            return None

        resp_len = header[3]

        # Read DATA(resp_len) + DCS(1) + POSTAMBLE(1)
        body = b""
        to_read = resp_len + 2
        while len(body) < to_read and time.monotonic() < deadline:
            remaining = to_read - len(body)
            self._serial.timeout = max(0.01, deadline - time.monotonic())
            chunk = self._serial.read(remaining)
            if chunk:
                body += chunk

        if len(body) < to_read:
            return None

        full_response = header + body

        # Log RX
        if logs is not None:
            logs.append({
                "time": self._timestamp(),
                "direction": "RX",
                "data": self._format_hex(full_response),
            })

        # Extract data payload (skip TFI byte)
        data_payload = body[:resp_len]
        if len(data_payload) < 1:
            return None

        # data_payload[0] is TFI (0xD5), data_payload[1] is response cmd
        return data_payload

    def _wakeup(self, logs=None):
        """Send wakeup preamble to bring PN532 out of HSU sleep.

        After the sync bytes, a dummy GetFirmwareVersion command is sent
        because the first real command after HSU wakeup is often ignored
        by the PN532.  This "sacrificial" command absorbs that quirk so
        the caller's next command succeeds reliably.
        """
        # PN532 HSU wakeup: 0x55 sync bytes + start code (0x00 0x00 0xFF)
        wakeup_bytes = b"\x55" * 16 + b"\x00\x00\xff"
        if logs is not None:
            logs.append({
                "time": self._timestamp(),
                "direction": "TX",
                "data": self._format_hex(wakeup_bytes),
            })
        self._serial.write(wakeup_bytes)
        self._serial.flush()
        time.sleep(0.2)
        self._serial.reset_input_buffer()

        # Sacrificial command — response may or may not arrive.
        self._send_command(0x02, logs=logs)
        self._serial.reset_input_buffer()

    def _ensure_open(self):
        """Open serial port if not already connected.

        On fresh open, performs an explicit DTR reset so the PN532
        always starts in a known state.  Reuses an existing connection
        if the port is already open and the underlying device file
        still exists.
        """
        if self._serial and self._serial.is_open:
            # Verify the device file still exists (catches USB unplug/replug)
            import os
            if os.path.exists(self._port):
                return
            # Device gone — close the stale handle so we can reopen
            self._close()
        self._serial = serial.Serial()
        self._serial.port = self._port
        self._serial.baudrate = self._baudrate
        self._serial.timeout = 1.0
        self._serial.dsrdtr = False
        self._serial.rtscts = False
        self._serial.dtr = False
        self._serial.open()
        # Explicit DTR reset: macOS may briefly pulse DTR during open(),
        # leaving the PN532 in an undefined state.  A full toggle here
        # guarantees a clean boot.
        self._serial.dtr = True   # RSTPDN LOW — assert reset
        time.sleep(0.1)
        self._serial.dtr = False  # RSTPDN HIGH — release, PN532 boots
        time.sleep(1.5)
        self._serial.reset_input_buffer()

    def _hard_reset(self):
        """Perform a DTR-based hardware reset of the PN532.

        Toggles DTR on the existing connection to avoid unpredictable
        DTR state changes caused by close/reopen on macOS.
        """
        self._ensure_open()
        self._serial.dtr = True   # RSTPDN LOW — assert reset
        time.sleep(0.5)
        self._serial.dtr = False  # RSTPDN HIGH — release, PN532 boots
        time.sleep(3.0)
        self._serial.reset_input_buffer()

    def _close(self):
        """Close serial port."""
        if self._serial:
            if self._serial.is_open:
                self._serial.close()
            self._serial = None

    # -- Command methods --

    def sam_configuration(self, logs=None, retries=3):
        """Configure SAM to normal mode (with retries for post-wakeup timing)."""
        for attempt in range(retries):
            resp = self._send_command(0x14, b"\x01\x00", logs=logs)
            if resp is not None:
                return resp
            # Flush stale data and wait before retrying
            self._serial.reset_input_buffer()
            time.sleep(0.1)

        # All soft retries failed — perform a DTR hard reset and retry.
        if logs is not None:
            logs.append({
                "time": self._timestamp(),
                "direction": "TX",
                "data": "(hard reset)",
            })
        self._hard_reset()
        self._wakeup(logs)

        for _ in range(retries):
            resp = self._send_command(0x14, b"\x01\x00", logs=logs)
            if resp is not None:
                return resp
            self._serial.reset_input_buffer()
            time.sleep(0.1)

        # Last resort: close and fully reopen the serial connection.
        # This recovers from stale file descriptors after USB replug.
        if logs is not None:
            logs.append({
                "time": self._timestamp(),
                "direction": "TX",
                "data": "(full reconnect)",
            })
        self._close()
        self._ensure_open()
        self._wakeup(logs)

        for _ in range(retries):
            resp = self._send_command(0x14, b"\x01\x00", logs=logs)
            if resp is not None:
                return resp
            self._serial.reset_input_buffer()
            time.sleep(0.1)
        return None

    def get_firmware_version(self, logs=None):
        """Get firmware version. Returns (IC, Ver, Rev, Support) or None."""
        resp = self._send_command(0x02, logs=logs)
        if resp and len(resp) >= 6:
            # resp: D5 03 IC Ver Rev Support
            return resp[2], resp[3], resp[4], resp[5]
        return None

    def rf_configuration(self, item, data, logs=None):
        """Set RF configuration."""
        return self._send_command(0x32, bytes([item]) + bytes(data), logs=logs)

    def in_list_passive_target(self, brty=0x00, timeout=3.0, logs=None):
        """
        Scan for passive targets.
        brty=0x00 → ISO14443A 106kbps
        Returns response data or None.
        """
        return self._send_command(0x4A, bytes([0x01, brty]), timeout=timeout, logs=logs)

    def in_data_exchange(self, tg, data, timeout=2.0, logs=None):
        """
        InDataExchange (0x40) — exchange data with an activated target.
        Returns response data (D5 41 Status DataOut...) or None.
        """
        params = bytes([tg]) + bytes(data)
        return self._send_command(0x40, params, timeout=timeout, logs=logs)

    def in_release(self, tg=0x00, logs=None):
        """Release target."""
        return self._send_command(0x44, bytes([tg]), logs=logs)

    def power_down(self, logs=None):
        """Enter power-down mode."""
        return self._send_command(0x16, b"\xF0", logs=logs)

    def set_parameters(self, flags, logs=None):
        """SetParameters (0x12). flags is a single byte."""
        return self._send_command(0x12, bytes([flags]), logs=logs)

    def tg_init_as_target(self, mode, mifare_params, felica_params,
                          nfcid3t, gt=b"", tk=b"", timeout=60.0, logs=None):
        """
        TgInitAsTarget (0x8C) — configure PN532 as a target.
        Blocks until a reader activates us or timeout expires.
        Returns response data or None.
        """
        params = (
            bytes([mode])
            + bytes(mifare_params)    # 6 bytes
            + bytes(felica_params)    # 18 bytes
            + bytes(nfcid3t)          # 10 bytes
            + bytes([len(gt)]) + bytes(gt)
            + bytes([len(tk)]) + bytes(tk)
        )
        return self._send_command(0x8C, params, timeout=timeout, logs=logs)

    def tg_get_data(self, timeout=60.0, logs=None):
        """
        TgGetData (0x86) — receive C-APDU from reader.
        Blocks until data arrives or timeout expires.
        Returns response data (D5 87 Status DataIn...) or None.
        """
        return self._send_command(0x86, b"", timeout=timeout, logs=logs)

    def tg_set_data(self, data, logs=None):
        """
        TgSetData (0x8E) — send R-APDU to reader.
        Returns response data or None.
        """
        return self._send_command(0x8E, bytes(data), logs=logs)

    # -- Response parsing --

    def _parse_14443a_target(self, resp):
        """
        Parse InListPassiveTarget response for ISO 14443A.
        resp bytes: D5 4B NbTg [Tg ATQA(2) SAK(1) NFCIDLength(1) UID(N) [ATS_Len ATS...]]
        """
        if resp is None or len(resp) < 3:
            return None

        # resp[0]=D5, resp[1]=4B, resp[2]=NbTg
        nb_tg = resp[2]
        if nb_tg == 0:
            return None

        # Parse first target
        if len(resp) < 8:
            return None

        offset = 3
        # tg = resp[offset]
        offset += 1

        atqa = resp[offset:offset + 2]
        offset += 2

        sak = resp[offset]
        offset += 1

        uid_len = resp[offset]
        offset += 1

        if len(resp) < offset + uid_len:
            return None

        uid = resp[offset:offset + uid_len]
        offset += uid_len

        # Check for ATS
        ats = b""
        if offset < len(resp):
            ats_len = resp[offset]
            if offset + ats_len <= len(resp):
                ats = resp[offset + 1:offset + ats_len]

        card = CardInfo(
            uid=" ".join(f"{b:02x}" for b in uid),
            atqa=" ".join(f"{b:02x}" for b in atqa),
            sak=f"{sak:02x}",
            ats=" ".join(f"{b:02x}" for b in ats) if ats else "",
        )
        return card

    # -- Top-level scan --

    def scan_type_a(self):
        """
        Perform a full ISO 14443A scan sequence.
        Returns dict with {success, cards, logs, raw_output}.
        """
        logs = []
        cards = []
        raw_lines = []

        with self._lock:
            try:
                self._ensure_open()

                # Wakeup
                self._wakeup(logs)

                # SAM Configuration (normal mode)
                resp = self.sam_configuration(logs)
                if resp is None:
                    return {
                        "success": False,
                        "error": "SAMConfiguration failed",
                        "cards": [],
                        "logs": logs,
                    }

                # Get firmware version
                fw = self.get_firmware_version(logs)
                device_name = "PN532"
                if fw:
                    ic, ver, rev, sup = fw
                    device_name = f"PN5{ic:02x} v{ver}.{rev}"
                    raw_lines.append(f"Firmware: {device_name}")

                # RF Configuration — MaxRetries
                self.rf_configuration(0x05, [0xFF, 0x01, 0xFF], logs)
                # Longer RF timeout for PN532-to-PN532 emulation scenarios
                self.rf_configuration(0x02, [0x00, 0x0B, 0x0E], logs)  # fRetryTimeout=0x0E (~819ms)

                # InListPassiveTarget — ISO14443A
                resp = self.in_list_passive_target(brty=0x00, timeout=3.0, logs=logs)
                card = self._parse_14443a_target(resp)

                if card:
                    card.device = device_name
                    cards.append({
                        "type": card.type,
                        "device": card.device,
                        "uid": card.uid,
                        "atqa": card.atqa,
                        "sak": card.sak,
                        "ats": card.ats if card.ats else None,
                    })
                    raw_lines.append(f"UID: {card.uid}")
                    raw_lines.append(f"ATQA: {card.atqa}")
                    raw_lines.append(f"SAK: {card.sak}")
                    if card.ats:
                        raw_lines.append(f"ATS: {card.ats}")

                    # Release target
                    self.in_release(logs=logs)

                # Power down
                self.power_down(logs)

                return {
                    "success": True,
                    "cards": cards,
                    "logs": logs,
                    "raw_output": "\n".join(raw_lines),
                }

            except serial.SerialException as e:
                self._close()
                return {
                    "success": False,
                    "error": f"Serial port error: {e}",
                    "cards": [],
                    "logs": logs,
                }
            except Exception as e:
                return {
                    "success": False,
                    "error": str(e),
                    "cards": [],
                    "logs": logs,
                }

    # -- Vault protocol reader --

    def read_vault_tag(self, read_offset=0, read_length=64):
        """
        Read data from a card implementing the Vault APDU protocol.
        Selects the Vault AID, then issues READ BINARY.
        Returns dict with {success, card_info, data_hex, data_text, logs}.
        """
        logs = []
        vault_aid = bytes([0xF0, 0x01, 0x02, 0x03, 0x04, 0x05])

        with self._lock:
            try:
                self._ensure_open()
                self._wakeup(logs)

                resp = self.sam_configuration(logs)
                if resp is None:
                    return {"success": False, "error": "SAMConfiguration failed", "logs": logs}

                # MaxRetries
                self.rf_configuration(0x05, [0xFF, 0x01, 0xFF], logs)
                # Longer RF timeout for PN532-to-PN532 emulation scenarios
                self.rf_configuration(0x02, [0x00, 0x0B, 0x0E], logs)  # fRetryTimeout=0x0E (~819ms)

                # Detect card
                resp = self.in_list_passive_target(brty=0x00, timeout=3.0, logs=logs)
                card = self._parse_14443a_target(resp)
                if card is None:
                    self.power_down(logs)
                    return {"success": False, "error": "No card detected", "logs": logs}

                card_info = {
                    "uid": card.uid,
                    "atqa": card.atqa,
                    "sak": card.sak,
                    "ats": card.ats if card.ats else None,
                }

                tg = 0x01

                # SELECT Vault AID: 00 A4 04 00 06 F0 01 02 03 04 05 00
                select_apdu = bytes([0x00, 0xA4, 0x04, 0x00, len(vault_aid)]) + vault_aid + bytes([0x00])
                sw1, sw2, _ = self._exchange_apdu(tg, select_apdu, logs)
                if (sw1, sw2) != (0x90, 0x00):
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"SELECT rejected (SW={sw1:02x}{sw2:02x})", "card_info": card_info, "logs": logs}

                # READ BINARY: 00 B0 00 <offset> <length>
                read_apdu = bytes([0x00, 0xB0, 0x00, read_offset & 0xFF, read_length & 0xFF])
                sw1, sw2, payload = self._exchange_apdu(tg, read_apdu, logs)

                data_hex = self._format_hex(payload)
                # Decode as text, replacing non-printable bytes
                data_text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in payload)

                self.in_release(logs=logs)
                self.power_down(logs)

                return {
                    "success": True,
                    "card_info": card_info,
                    "data_hex": data_hex,
                    "data_text": data_text,
                    "logs": logs,
                }

            except serial.SerialException as e:
                self._close()
                return {"success": False, "error": f"Serial port error: {e}", "logs": logs}
            except Exception as e:
                return {"success": False, "error": str(e), "logs": logs}

    def write_vault_tag(self, write_offset, data_bytes):
        """
        Write data to a card implementing the Vault APDU protocol.
        Selects the Vault AID, then issues WRITE (INS=0xD0).
        Returns dict with {success, card_info, logs}.
        """
        logs = []
        vault_aid = bytes([0xF0, 0x01, 0x02, 0x03, 0x04, 0x05])

        with self._lock:
            try:
                self._ensure_open()
                self._wakeup(logs)

                resp = self.sam_configuration(logs)
                if resp is None:
                    return {"success": False, "error": "SAMConfiguration failed", "logs": logs}

                self.rf_configuration(0x05, [0xFF, 0x01, 0xFF], logs)
                # Longer RF timeout for PN532-to-PN532 emulation scenarios
                self.rf_configuration(0x02, [0x00, 0x0B, 0x0E], logs)  # fRetryTimeout=0x0E (~819ms)

                resp = self.in_list_passive_target(brty=0x00, timeout=3.0, logs=logs)
                card = self._parse_14443a_target(resp)
                if card is None:
                    self.power_down(logs)
                    return {"success": False, "error": "No card detected", "logs": logs}

                card_info = {
                    "uid": card.uid,
                    "atqa": card.atqa,
                    "sak": card.sak,
                    "ats": card.ats if card.ats else None,
                }

                tg = 0x01

                # SELECT Vault AID
                select_apdu = bytes([0x00, 0xA4, 0x04, 0x00, len(vault_aid)]) + vault_aid + bytes([0x00])
                sw1, sw2, _ = self._exchange_apdu(tg, select_apdu, logs)
                if (sw1, sw2) != (0x90, 0x00):
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"SELECT rejected (SW={sw1:02x}{sw2:02x})", "card_info": card_info, "logs": logs}

                # WRITE: 00 D0 00 <offset> <length> <data>
                write_apdu = bytes([0x00, 0xD0, 0x00, write_offset & 0xFF, len(data_bytes)]) + data_bytes
                sw1, sw2, _ = self._exchange_apdu(tg, write_apdu, logs)

                self.in_release(logs=logs)
                self.power_down(logs)

                if (sw1, sw2) != (0x90, 0x00):
                    return {"success": False, "error": f"WRITE failed (SW={sw1:02x}{sw2:02x})", "card_info": card_info, "logs": logs}

                return {
                    "success": True,
                    "card_info": card_info,
                    "bytes_written": len(data_bytes),
                    "logs": logs,
                }

            except serial.SerialException as e:
                self._close()
                return {"success": False, "error": f"Serial port error: {e}", "logs": logs}
            except Exception as e:
                return {"success": False, "error": str(e), "logs": logs}

    # -- NDEF Type 4 reader --

    def _exchange_apdu(self, tg, apdu, logs, retries=1):
        """
        Send an APDU via InDataExchange.
        Handles ISO-DEP CID framing leak (PN532-to-PN532 workaround)
        and retries on short responses (e.g. HCE routing delay).
        Returns (sw1, sw2, payload) or raises RuntimeError on failure.
        """
        for attempt in range(1 + retries):
            # Small delay between consecutive APDUs to give emulated cards
            # (PN532 target) time to loop back from TgSetData to TgGetData.
            time.sleep(0.02)
            resp = self.in_data_exchange(tg, apdu, timeout=2.0, logs=logs)
            if resp is None or len(resp) < 3:
                raise RuntimeError("No response from card")
            status = resp[2]
            if status != 0x00:
                if attempt < retries:
                    time.sleep(0.05)
                    continue
                raise RuntimeError(f"InDataExchange error 0x{status:02x}")
            data = resp[3:]
            # Workaround: PN532 may leak raw ISO-DEP I-block when CID is present.
            # PCB byte 0x0A/0x0B/0x08/0x09 = I-block with CID bit set (bit 3).
            # Skip PCB + 1 CID byte to extract the actual APDU response.
            if len(data) >= 3 and (data[0] & 0xE8) == 0x08:
                data = data[2:]  # skip PCB + CID byte
            if len(data) >= 2:
                sw1, sw2 = data[-2], data[-1]
                payload = data[:-2]
                return sw1, sw2, payload
            # Short response — retry after brief delay
            if attempt < retries:
                time.sleep(0.1)
        raise RuntimeError(f"Response too short ({len(data)} bytes, data=0x{self._format_hex(data)})")

    def read_ndef_tag(self):
        """
        Read NDEF message from a Type 4 Tag.
        Follows NFC Forum Type 4 Tag Operation:
          1. SELECT NDEF Tag Application (AID D2 76 00 00 85 01 01)
          2. SELECT CC file (E103), READ CC to find NDEF file info
          3. SELECT NDEF file, READ length prefix, READ message body
        Returns dict with {success, card_info, ndef_records, raw_hex, logs}.
        """
        logs = []
        ndef_aid = bytes([0xD2, 0x76, 0x00, 0x00, 0x85, 0x01, 0x01])

        with self._lock:
            try:
                self._ensure_open()
                self._wakeup(logs)

                resp = self.sam_configuration(logs)
                if resp is None:
                    return {"success": False, "error": "SAMConfiguration failed", "logs": logs}

                self.rf_configuration(0x05, [0xFF, 0x01, 0xFF], logs)
                # Longer RF timeout for PN532-to-PN532 emulation scenarios
                self.rf_configuration(0x02, [0x00, 0x0B, 0x0E], logs)  # fRetryTimeout=0x0E (~819ms)

                # Detect card
                resp = self.in_list_passive_target(brty=0x00, timeout=3.0, logs=logs)
                card = self._parse_14443a_target(resp)
                if card is None:
                    self.power_down(logs)
                    return {"success": False, "error": "No card detected", "logs": logs}

                card_info = {
                    "uid": card.uid,
                    "atqa": card.atqa,
                    "sak": card.sak,
                    "ats": card.ats if card.ats else None,
                }

                tg = 0x01

                # 1) SELECT NDEF Tag Application
                select_aid = bytes([0x00, 0xA4, 0x04, 0x00, len(ndef_aid)]) + ndef_aid + bytes([0x00])
                sw1, sw2, _ = self._exchange_apdu(tg, select_aid, logs)
                if (sw1, sw2) != (0x90, 0x00):
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"SELECT NDEF AID failed (SW={sw1:02x}{sw2:02x})",
                            "card_info": card_info, "logs": logs}

                # 2) SELECT CC file (E103)
                select_cc = bytes([0x00, 0xA4, 0x00, 0x0C, 0x02, 0xE1, 0x03])
                sw1, sw2, _ = self._exchange_apdu(tg, select_cc, logs)
                if (sw1, sw2) != (0x90, 0x00):
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"SELECT CC failed (SW={sw1:02x}{sw2:02x})",
                            "card_info": card_info, "logs": logs}

                # READ CC (15 bytes)
                read_cc = bytes([0x00, 0xB0, 0x00, 0x00, 0x0F])
                sw1, sw2, cc_data = self._exchange_apdu(tg, read_cc, logs)
                if (sw1, sw2) != (0x90, 0x00) or len(cc_data) < 15:
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": "READ CC failed",
                            "card_info": card_info, "logs": logs}

                # Parse CC: bytes 9-10 = NDEF file ID, bytes 11-12 = max NDEF size
                ndef_file_id_hi = cc_data[9]
                ndef_file_id_lo = cc_data[10]
                ndef_max_size = (cc_data[11] << 8) | cc_data[12]

                # 3) SELECT NDEF file
                select_ndef = bytes([0x00, 0xA4, 0x00, 0x0C, 0x02, ndef_file_id_hi, ndef_file_id_lo])
                sw1, sw2, _ = self._exchange_apdu(tg, select_ndef, logs)
                if (sw1, sw2) != (0x90, 0x00):
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"SELECT NDEF file failed (SW={sw1:02x}{sw2:02x})",
                            "card_info": card_info, "logs": logs}

                # READ first 2 bytes — NDEF message length
                read_len = bytes([0x00, 0xB0, 0x00, 0x00, 0x02])
                sw1, sw2, len_data = self._exchange_apdu(tg, read_len, logs)
                if (sw1, sw2) != (0x90, 0x00) or len(len_data) < 2:
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": "READ NDEF length failed",
                            "card_info": card_info, "logs": logs}

                ndef_msg_len = (len_data[0] << 8) | len_data[1]
                if ndef_msg_len == 0:
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": True, "card_info": card_info,
                            "ndef_records": [], "raw_hex": "", "logs": logs}

                # READ NDEF message body (offset=2, chunked if needed)
                ndef_bytes = b""
                offset = 2
                remaining = ndef_msg_len
                max_read = 59  # typical MLe for Type 4 Tags
                while remaining > 0:
                    chunk_size = min(remaining, max_read)
                    read_msg = bytes([0x00, 0xB0, (offset >> 8) & 0xFF, offset & 0xFF, chunk_size])
                    sw1, sw2, chunk = self._exchange_apdu(tg, read_msg, logs)
                    if (sw1, sw2) != (0x90, 0x00):
                        break
                    ndef_bytes += bytes(chunk)
                    offset += len(chunk)
                    remaining -= len(chunk)

                self.in_release(logs=logs)
                self.power_down(logs)

                # Parse NDEF records
                raw_hex = self._format_hex(ndef_bytes)
                ndef_records = []
                try:
                    import ndef as ndef_lib
                    for record in ndef_lib.message_decoder(ndef_bytes):
                        rec_info = {"type": record.type, "tnf": record._type_name_format}
                        if hasattr(record, 'uri'):
                            rec_info["value"] = record.uri
                        elif hasattr(record, 'text'):
                            rec_info["value"] = record.text
                        else:
                            rec_info["value"] = self._format_hex(record.data) if hasattr(record, 'data') else str(record)
                        ndef_records.append(rec_info)
                except Exception:
                    ndef_records = [{"type": "raw", "value": raw_hex}]

                return {
                    "success": True,
                    "card_info": card_info,
                    "ndef_records": ndef_records,
                    "raw_hex": raw_hex,
                    "logs": logs,
                }

            except RuntimeError as e:
                return {"success": False, "error": str(e), "card_info": card_info if 'card_info' in dir() else None, "logs": logs}
            except serial.SerialException as e:
                self._close()
                return {"success": False, "error": f"Serial port error: {e}", "logs": logs}
            except Exception as e:
                return {"success": False, "error": str(e), "logs": logs}

    def write_ndef_tag(self, ndef_msg_bytes):
        """
        Write an NDEF message to a Type 4 Tag.
        Follows NFC Forum Type 4 Tag Operation:
          1. SELECT NDEF Tag Application
          2. SELECT CC, READ CC to check write access and max size
          3. SELECT NDEF file
          4. UPDATE BINARY: set length to 0 (mark empty during write)
          5. UPDATE BINARY: write NDEF message body (chunked)
          6. UPDATE BINARY: set actual length
        Returns dict with {success, card_info, logs}.
        """
        logs = []
        ndef_aid = bytes([0xD2, 0x76, 0x00, 0x00, 0x85, 0x01, 0x01])

        with self._lock:
            try:
                self._ensure_open()
                self._wakeup(logs)

                resp = self.sam_configuration(logs)
                if resp is None:
                    return {"success": False, "error": "SAMConfiguration failed", "logs": logs}

                self.rf_configuration(0x05, [0xFF, 0x01, 0xFF], logs)
                # Longer RF timeout for PN532-to-PN532 emulation scenarios
                self.rf_configuration(0x02, [0x00, 0x0B, 0x0E], logs)  # fRetryTimeout=0x0E (~819ms)

                resp = self.in_list_passive_target(brty=0x00, timeout=3.0, logs=logs)
                card = self._parse_14443a_target(resp)
                if card is None:
                    self.power_down(logs)
                    return {"success": False, "error": "No card detected", "logs": logs}

                card_info = {
                    "uid": card.uid,
                    "atqa": card.atqa,
                    "sak": card.sak,
                    "ats": card.ats if card.ats else None,
                }

                tg = 0x01

                # 1) SELECT NDEF Tag Application
                select_aid = bytes([0x00, 0xA4, 0x04, 0x00, len(ndef_aid)]) + ndef_aid + bytes([0x00])
                sw1, sw2, _ = self._exchange_apdu(tg, select_aid, logs)
                if (sw1, sw2) != (0x90, 0x00):
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"SELECT NDEF AID failed (SW={sw1:02x}{sw2:02x})",
                            "card_info": card_info, "logs": logs}

                # 2) SELECT CC file (E103), READ CC
                select_cc = bytes([0x00, 0xA4, 0x00, 0x0C, 0x02, 0xE1, 0x03])
                sw1, sw2, _ = self._exchange_apdu(tg, select_cc, logs)
                if (sw1, sw2) != (0x90, 0x00):
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"SELECT CC failed (SW={sw1:02x}{sw2:02x})",
                            "card_info": card_info, "logs": logs}

                read_cc = bytes([0x00, 0xB0, 0x00, 0x00, 0x0F])
                sw1, sw2, cc_data = self._exchange_apdu(tg, read_cc, logs)
                if (sw1, sw2) != (0x90, 0x00) or len(cc_data) < 15:
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": "READ CC failed",
                            "card_info": card_info, "logs": logs}

                # Parse CC
                ndef_file_id_hi = cc_data[9]
                ndef_file_id_lo = cc_data[10]
                ndef_max_size = (cc_data[11] << 8) | cc_data[12]
                write_access = cc_data[14]

                if write_access != 0x00:
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"Write access denied (0x{write_access:02x})",
                            "card_info": card_info, "logs": logs}

                # NDEF file = 2-byte length prefix + message body
                ndef_file_content = struct.pack(">H", len(ndef_msg_bytes)) + ndef_msg_bytes
                if len(ndef_file_content) > ndef_max_size:
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"Message too large ({len(ndef_msg_bytes)} bytes, max {ndef_max_size - 2})",
                            "card_info": card_info, "logs": logs}

                # 3) SELECT NDEF file
                select_ndef = bytes([0x00, 0xA4, 0x00, 0x0C, 0x02, ndef_file_id_hi, ndef_file_id_lo])
                sw1, sw2, _ = self._exchange_apdu(tg, select_ndef, logs)
                if (sw1, sw2) != (0x90, 0x00):
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"SELECT NDEF file failed (SW={sw1:02x}{sw2:02x})",
                            "card_info": card_info, "logs": logs}

                # 4) UPDATE BINARY: write length = 0 (mark empty during write)
                update_zero = bytes([0x00, 0xD6, 0x00, 0x00, 0x02, 0x00, 0x00])
                sw1, sw2, _ = self._exchange_apdu(tg, update_zero, logs)
                if (sw1, sw2) != (0x90, 0x00):
                    self.in_release(logs=logs)
                    self.power_down(logs)
                    return {"success": False, "error": f"UPDATE BINARY (reset length) failed (SW={sw1:02x}{sw2:02x})",
                            "card_info": card_info, "logs": logs}

                # 5) UPDATE BINARY: write NDEF message body (offset=2, chunked)
                mlc = (cc_data[5] << 8) | cc_data[6]  # max write size from CC
                max_write = min(mlc, 52)  # conservative chunk size
                offset = 2
                remaining_data = ndef_msg_bytes
                while remaining_data:
                    chunk = remaining_data[:max_write]
                    remaining_data = remaining_data[max_write:]
                    update_cmd = bytes([0x00, 0xD6, (offset >> 8) & 0xFF, offset & 0xFF, len(chunk)]) + chunk
                    sw1, sw2, _ = self._exchange_apdu(tg, update_cmd, logs)
                    if (sw1, sw2) != (0x90, 0x00):
                        self.in_release(logs=logs)
                        self.power_down(logs)
                        return {"success": False, "error": f"UPDATE BINARY (data) failed at offset {offset} (SW={sw1:02x}{sw2:02x})",
                                "card_info": card_info, "logs": logs}
                    offset += len(chunk)

                # 6) UPDATE BINARY: write actual NDEF message length
                len_bytes = struct.pack(">H", len(ndef_msg_bytes))
                update_len = bytes([0x00, 0xD6, 0x00, 0x00, 0x02]) + len_bytes
                sw1, sw2, _ = self._exchange_apdu(tg, update_len, logs)

                self.in_release(logs=logs)
                self.power_down(logs)

                if (sw1, sw2) != (0x90, 0x00):
                    return {"success": False, "error": f"UPDATE BINARY (set length) failed (SW={sw1:02x}{sw2:02x})",
                            "card_info": card_info, "logs": logs}

                return {
                    "success": True,
                    "card_info": card_info,
                    "bytes_written": len(ndef_msg_bytes),
                    "logs": logs,
                }

            except RuntimeError as e:
                return {"success": False, "error": str(e), "card_info": card_info if 'card_info' in dir() else None, "logs": logs}
            except serial.SerialException as e:
                self._close()
                return {"success": False, "error": f"Serial port error: {e}", "logs": logs}
            except Exception as e:
                return {"success": False, "error": str(e), "logs": logs}

    # -- Type 4 Tag emulation --

    def emulate_tag(self, emulator, stop_event, logs):
        """
        Emulate an NFC tag using the given APDU emulator.
        Runs until stop_event is set or an unrecoverable error occurs.
        Appends log dicts to the ``logs`` deque (thread-safe via caller's lock).

        emulator: object with handle_apdu(apdu) -> bytes method.
        stop_event: threading.Event signalling when to stop.
        logs: collections.deque to append log entries to.
        """

        # TgInitAsTarget parameters for ISO14443-4 PICC
        mode = 0x05  # PassiveOnly | PICCOnly
        mifare_params = bytes([
            0x04, 0x00,        # SENS_RES (ATQA)
            0x01, 0x02, 0x03,  # NFCID1t (3-byte UID)
            0x20,              # SEL_RES (SAK — ISO14443-4 compliant)
        ])
        felica_params = bytes(18)  # not used
        nfcid3t = bytes([0x01, 0x02, 0x03, 0x04, 0x05,
                         0x06, 0x07, 0x08, 0x09, 0x0A])
        # ATS Historical Bytes — needed for Android to recognize Type 4 Tag.
        # Format: category indicator 0x80 (status indicator only, no TLV data)
        tk = bytes([0x80])

        with self._lock:
            try:
                self._ensure_open()
                self._wakeup(logs)
                self.sam_configuration(logs)
                self.set_parameters(0x24, logs)  # fAutomaticATR_RES | fISO14443-4_PICC

                while not stop_event.is_set():
                    # Wait for reader activation
                    resp = self.tg_init_as_target(
                        mode, mifare_params, felica_params, nfcid3t,
                        tk=tk, timeout=2.0, logs=logs,
                    )
                    if resp is None:
                        # Timeout — no reader yet, loop and retry
                        continue

                    # Activated — handle APDU exchange loop
                    consecutive_timeouts = 0
                    while not stop_event.is_set():
                        resp = self.tg_get_data(timeout=2.0, logs=logs)
                        if resp is None:
                            consecutive_timeouts += 1
                            if consecutive_timeouts >= 3:
                                # Reader likely disconnected, re-init as target
                                break
                            continue
                        consecutive_timeouts = 0

                        # resp: D5 87 Status [DataIn...]
                        if len(resp) < 3:
                            break
                        status = resp[2]
                        if status != 0x00:
                            # 0x29 = target released by initiator
                            break

                        c_apdu = resp[3:]
                        r_apdu = emulator.handle_apdu(c_apdu)
                        send_resp = self.tg_set_data(r_apdu, logs=logs)
                        if send_resp is None:
                            break
                        # Check TgSetData status
                        if len(send_resp) >= 3 and send_resp[2] != 0x00:
                            break

            except serial.SerialException as e:
                self._close()
                logs.append({
                    "time": self._timestamp(),
                    "direction": "ERR",
                    "data": f"Serial error: {e}",
                })
            except Exception as e:
                logs.append({
                    "time": self._timestamp(),
                    "direction": "ERR",
                    "data": f"Error: {e}",
                })


class Type4TagEmulator:
    """
    NFC Forum Type 4 Tag APDU handler.

    Implements a minimal virtual file system with a Capability Container (CC)
    and an NDEF file, responding to SELECT and READ BINARY APDUs.
    """

    CC_FILE_ID = 0xE103
    NDEF_FILE_ID = 0xE104

    def __init__(self, ndef_message_bytes):
        self._ndef_msg = bytes(ndef_message_bytes)
        # NDEF file contents: 2-byte big-endian length prefix + message
        self._ndef_file = struct.pack(">H", len(self._ndef_msg)) + self._ndef_msg
        ndef_max_size = len(self._ndef_file)

        # Capability Container (15 bytes)
        self._cc_file = bytes([
            0x00, 0x0F,        # CC length
            0x20,              # Mapping version 2.0
            0x00, 0x3B,        # MLe (max read = 59)
            0x00, 0x34,        # MLc (max write = 52)
            0x04, 0x06,        # NDEF File Control TLV: type=0x04, length=6
            0xE1, 0x04,        # NDEF file ID
        ]) + struct.pack(">H", ndef_max_size) + bytes([
            0x00,              # Read access: free
            0xFF,              # Write access: denied
        ])

        self._selected_file = None

    def handle_apdu(self, apdu):
        """
        Process a C-APDU and return the R-APDU bytes.
        """
        if len(apdu) < 4:
            return bytes([0x6D, 0x00])  # INS not supported

        cla, ins, p1, p2 = apdu[0], apdu[1], apdu[2], apdu[3]

        if ins == 0xA4:  # SELECT
            return self._handle_select(apdu)
        elif ins == 0xB0:  # READ BINARY
            return self._handle_read_binary(apdu)
        elif ins == 0xD6:  # UPDATE BINARY
            return bytes([0x6A, 0x82])  # write denied
        else:
            return bytes([0x6D, 0x00])  # INS not supported

    def _handle_select(self, apdu):
        """Handle SELECT command."""
        if len(apdu) < 5:
            return bytes([0x6A, 0x82])

        p1, p2 = apdu[2], apdu[3]
        lc = apdu[4]
        data = apdu[5:5 + lc]

        # SELECT by AID (P1=0x04)
        if p1 == 0x04:
            ndef_aid = bytes([0xD2, 0x76, 0x00, 0x00, 0x85, 0x01, 0x01])
            if data == ndef_aid:
                return bytes([0x90, 0x00])
            return bytes([0x6A, 0x82])  # application not found

        # SELECT by File ID (P1=0x00)
        if p1 == 0x00 and len(data) == 2:
            file_id = (data[0] << 8) | data[1]
            if file_id == self.CC_FILE_ID:
                self._selected_file = self._cc_file
                return bytes([0x90, 0x00])
            elif file_id == self.NDEF_FILE_ID:
                self._selected_file = self._ndef_file
                return bytes([0x90, 0x00])
            return bytes([0x6A, 0x82])  # file not found

        return bytes([0x6A, 0x82])

    def _handle_read_binary(self, apdu):
        """Handle READ BINARY command."""
        if self._selected_file is None:
            return bytes([0x6A, 0x82])  # no file selected

        offset = (apdu[2] << 8) | apdu[3]
        le = apdu[4] if len(apdu) > 4 else 0

        if offset > len(self._selected_file):
            return bytes([0x6A, 0x82])

        chunk = self._selected_file[offset:offset + le]
        return chunk + bytes([0x90, 0x00])


class VaultTagEmulator:
    """
    Vault APDU protocol emulator with a flat read/write data buffer.

    Supports SELECT (by Vault AID), READ BINARY, and WRITE (INS=0xD0).
    """

    VAULT_AID = bytes([0xF0, 0x01, 0x02, 0x03, 0x04, 0x05])
    BUFFER_SIZE = 256

    def __init__(self, initial_data=b""):
        self._buffer = bytearray(self.BUFFER_SIZE)
        n = min(len(initial_data), self.BUFFER_SIZE)
        self._buffer[:n] = initial_data[:n]
        self._selected = False

    def handle_apdu(self, apdu):
        """Process a C-APDU and return the R-APDU bytes."""
        if len(apdu) < 4:
            return bytes([0x6D, 0x00])  # INS not supported

        ins = apdu[1]
        if ins == 0xA4:   # SELECT
            return self._handle_select(apdu)
        elif ins == 0xB0:  # READ BINARY
            return self._handle_read(apdu)
        elif ins == 0xD0:  # WRITE (Vault)
            return self._handle_write(apdu)
        else:
            return bytes([0x6D, 0x00])  # INS not supported

    def _handle_select(self, apdu):
        """Handle SELECT command — match Vault AID."""
        if len(apdu) < 5:
            return bytes([0x6A, 0x82])

        p1 = apdu[2]
        lc = apdu[4]
        data = apdu[5:5 + lc]

        if p1 == 0x04 and data == self.VAULT_AID:
            self._selected = True
            return bytes([0x90, 0x00])

        return bytes([0x6A, 0x82])  # application not found

    def _handle_read(self, apdu):
        """Handle READ BINARY — P2=offset, Le=length."""
        if not self._selected:
            return bytes([0x6A, 0x82])

        offset = apdu[3]
        le = apdu[4] if len(apdu) > 4 else 0

        if offset >= self.BUFFER_SIZE:
            return bytes([0x6A, 0x82])

        chunk = bytes(self._buffer[offset:offset + le])
        return chunk + bytes([0x90, 0x00])

    def _handle_write(self, apdu):
        """Handle WRITE (INS=0xD0) — P2=offset, data follows."""
        if not self._selected:
            return bytes([0x6A, 0x82])

        offset = apdu[3]
        if len(apdu) < 5:
            return bytes([0x67, 0x00])  # wrong length

        lc = apdu[4]
        data = apdu[5:5 + lc]

        if offset + len(data) > self.BUFFER_SIZE:
            return bytes([0x6A, 0x82])  # out of range

        self._buffer[offset:offset + len(data)] = data
        return bytes([0x90, 0x00])
