"""
PN532 direct serial communication for ISO 14443A card scanning.
"""

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
        """Send wakeup preamble to bring PN532 out of sleep."""
        wakeup_bytes = b"\x55" * 16
        if logs is not None:
            logs.append({
                "time": self._timestamp(),
                "direction": "TX",
                "data": self._format_hex(wakeup_bytes),
            })
        self._serial.write(wakeup_bytes)
        self._serial.flush()
        time.sleep(0.01)
        self._serial.reset_input_buffer()

    def _open(self):
        """Open serial port."""
        self._serial = serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            timeout=1.0,
        )

    def _close(self):
        """Close serial port."""
        if self._serial and self._serial.is_open:
            self._serial.close()
            self._serial = None

    # -- Command methods --

    def sam_configuration(self, logs=None):
        """Configure SAM to normal mode."""
        return self._send_command(0x14, b"\x01\x00", logs=logs)

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
