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

SERIAL_PORT = "/dev/tty.usbserial-1410"
BAUDRATE = 115200


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

    def __init__(self, port=SERIAL_PORT, baudrate=BAUDRATE):
        self._port = port
        self._baudrate = baudrate
        self._serial = None
        self._lock = threading.Lock()

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
        """Send wakeup preamble to bring PN532 out of HSU sleep."""
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

    def _open(self):
        """Open serial port without toggling DTR (which resets PN532)."""
        self._serial = serial.Serial()
        self._serial.port = self._port
        self._serial.baudrate = self._baudrate
        self._serial.timeout = 1.0
        self._serial.dsrdtr = False
        self._serial.rtscts = False
        self._serial.dtr = False
        self._serial.open()
        time.sleep(0.05)
        self._serial.reset_input_buffer()

    def _close(self):
        """Close serial port."""
        if self._serial and self._serial.is_open:
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
                self._open()

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
                self.rf_configuration(0x05, [0xFF, 0x01, 0x02], logs)

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
            finally:
                self._close()

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
                self._open()
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
                    while not stop_event.is_set():
                        resp = self.tg_get_data(timeout=2.0, logs=logs)
                        if resp is None:
                            # Timeout waiting for data, re-check stop
                            continue

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
            finally:
                self._close()


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


class CustomTagEmulator:
    """
    Custom APDU protocol emulator with a flat read/write data buffer.

    Supports SELECT (by custom AID), READ BINARY, and WRITE (INS=0xD0).
    """

    CUSTOM_AID = bytes([0xF0, 0x01, 0x02, 0x03, 0x04, 0x05])
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
        elif ins == 0xD0:  # WRITE (custom)
            return self._handle_write(apdu)
        else:
            return bytes([0x6D, 0x00])  # INS not supported

    def _handle_select(self, apdu):
        """Handle SELECT command — match custom AID."""
        if len(apdu) < 5:
            return bytes([0x6A, 0x82])

        p1 = apdu[2]
        lc = apdu[4]
        data = apdu[5:5 + lc]

        if p1 == 0x04 and data == self.CUSTOM_AID:
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
