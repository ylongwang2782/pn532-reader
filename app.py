#!/usr/bin/env python3
"""
Web interface for PN532 NFC reader using libnfc
"""

import subprocess
import re
import os
import signal
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
import ndef
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# Store emulation process and logs
emulation_process = None
ndef_file_path = None
log_buffer = deque(maxlen=500)
log_lock = threading.Lock()
log_reader_thread = None


def parse_nfc_list_output(output: str) -> list[dict]:
    """Parse nfc-list output and extract card information."""
    cards = []
    current_card = None
    device_name = None

    for line in output.split('\n'):
        if 'NFC device:' in line and 'opened' in line:
            match = re.search(r'NFC device: (.+?) opened', line)
            if match:
                device_name = match.group(1)

        if 'ISO/IEC 14443A' in line or 'ISO/IEC 14443B' in line:
            if current_card:
                cards.append(current_card)
            current_card = {
                'type': line.strip(),
                'device': device_name,
                'atqa': None,
                'uid': None,
                'sak': None,
                'ats': None
            }

        if current_card:
            if 'ATQA' in line:
                match = re.search(r':\s*(.+)', line)
                if match:
                    current_card['atqa'] = match.group(1).strip()
            elif 'UID' in line or 'NFCID' in line:
                match = re.search(r':\s*(.+)', line)
                if match:
                    current_card['uid'] = match.group(1).strip()
            elif 'SAK' in line:
                match = re.search(r':\s*(.+)', line)
                if match:
                    current_card['sak'] = match.group(1).strip()
            elif 'ATS' in line:
                match = re.search(r':\s*(.+)', line)
                if match:
                    current_card['ats'] = match.group(1).strip()

    if current_card:
        cards.append(current_card)

    seen_uids = set()
    unique_cards = []
    for card in cards:
        if card['uid'] and card['uid'] not in seen_uids:
            seen_uids.add(card['uid'])
            unique_cards.append(card)

    return unique_cards


def parse_log_line(line: str) -> dict | None:
    """Parse a libnfc debug log line and extract TX/RX data."""
    timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]

    # Match TX/RX patterns
    tx_match = re.search(r'TX:\s*(.+)', line)
    rx_match = re.search(r'RX:\s*(.+)', line)

    if tx_match:
        return {
            'time': timestamp,
            'direction': 'TX',
            'data': tx_match.group(1).strip(),
            'raw': line.strip()
        }
    elif rx_match:
        return {
            'time': timestamp,
            'direction': 'RX',
            'data': rx_match.group(1).strip(),
            'raw': line.strip()
        }

    return None


def log_reader(process):
    """Read logs from process stderr in a separate thread."""
    global log_buffer

    try:
        for line in iter(process.stderr.readline, b''):
            if not line:
                break
            try:
                decoded = line.decode('utf-8', errors='replace').strip()
                if decoded:
                    parsed = parse_log_line(decoded)
                    if parsed:
                        with log_lock:
                            log_buffer.append(parsed)
            except Exception:
                pass
    except Exception:
        pass


def run_nfc_list() -> dict:
    """Run nfc-list command and return parsed results."""
    try:
        env = os.environ.copy()
        env['LIBNFC_LOG_LEVEL'] = '3'

        result = subprocess.run(
            ['nfc-list'],
            capture_output=True,
            text=True,
            timeout=10,
            env=env
        )

        output = result.stdout + result.stderr
        cards = parse_nfc_list_output(output)

        # Parse logs from output
        logs = []
        for line in output.split('\n'):
            parsed = parse_log_line(line)
            if parsed:
                logs.append(parsed)

        return {
            'success': True,
            'cards': cards,
            'raw_output': output,
            'logs': logs
        }
    except subprocess.TimeoutExpired:
        return {
            'success': False,
            'error': 'Command timed out',
            'cards': [],
            'logs': []
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'cards': [],
            'logs': []
        }


def create_ndef_file(ndef_type: str, content: str) -> str:
    """Create NDEF file and return path."""
    if ndef_type == 'url':
        record = ndef.UriRecord(content)
    elif ndef_type == 'text':
        record = ndef.TextRecord(content)
    else:
        raise ValueError(f"Unknown NDEF type: {ndef_type}")

    fd, path = tempfile.mkstemp(suffix='.ndef')
    with os.fdopen(fd, 'wb') as f:
        f.write(b''.join(ndef.message_encoder([record])))

    return path


@app.route('/')
def index():
    """Render the main page."""
    return render_template('index.html')


@app.route('/api/scan')
def scan():
    """Scan for NFC cards."""
    result = run_nfc_list()
    return jsonify(result)


@app.route('/api/emulate/start', methods=['POST'])
def emulate_start():
    """Start Type 4 tag emulation."""
    global emulation_process, ndef_file_path, log_buffer, log_reader_thread

    if emulation_process and emulation_process.poll() is None:
        return jsonify({
            'success': False,
            'error': 'Emulation already running'
        })

    data = request.get_json()
    ndef_type = data.get('type', 'text')
    content = data.get('content', '')

    if not content:
        return jsonify({
            'success': False,
            'error': 'Content is required'
        })

    try:
        ndef_file_path = create_ndef_file(ndef_type, content)

        # Clear log buffer
        with log_lock:
            log_buffer.clear()

        # Set environment for debug logging
        env = os.environ.copy()
        env['LIBNFC_LOG_LEVEL'] = '3'

        emulation_process = subprocess.Popen(
            ['nfc-emulate-forum-tag4', ndef_file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            preexec_fn=os.setsid
        )

        # Start log reader thread
        log_reader_thread = threading.Thread(
            target=log_reader,
            args=(emulation_process,),
            daemon=True
        )
        log_reader_thread.start()

        return jsonify({
            'success': True,
            'message': f'Emulating Type 4 tag with {ndef_type}: {content}',
            'pid': emulation_process.pid
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/emulate/stop', methods=['POST'])
def emulate_stop():
    """Stop Type 4 tag emulation."""
    global emulation_process, ndef_file_path

    if not emulation_process:
        return jsonify({
            'success': False,
            'error': 'No emulation running'
        })

    try:
        os.killpg(os.getpgid(emulation_process.pid), signal.SIGTERM)
        emulation_process.wait(timeout=5)
    except Exception:
        try:
            emulation_process.kill()
        except Exception:
            pass

    emulation_process = None

    if ndef_file_path and os.path.exists(ndef_file_path):
        os.remove(ndef_file_path)
        ndef_file_path = None

    return jsonify({
        'success': True,
        'message': 'Emulation stopped'
    })


@app.route('/api/emulate/status')
def emulate_status():
    """Get emulation status."""
    global emulation_process

    if emulation_process and emulation_process.poll() is None:
        return jsonify({
            'running': True,
            'pid': emulation_process.pid
        })

    return jsonify({
        'running': False
    })


@app.route('/api/logs')
def get_logs():
    """Get communication logs."""
    global log_buffer

    since_index = request.args.get('since', 0, type=int)

    with log_lock:
        logs = list(log_buffer)

    # Return logs after the specified index
    if since_index > 0 and since_index < len(logs):
        logs = logs[since_index:]

    return jsonify({
        'logs': logs,
        'total': len(log_buffer)
    })


@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    """Clear communication logs."""
    global log_buffer

    with log_lock:
        log_buffer.clear()

    return jsonify({'success': True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
