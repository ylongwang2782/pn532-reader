#!/usr/bin/env python3
"""
Web interface for PN532 NFC reader using libnfc
"""

import subprocess
import re
import os
import signal
import tempfile
import ndef
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# Store emulation process
emulation_process = None
ndef_file_path = None


def parse_nfc_list_output(output: str) -> list[dict]:
    """Parse nfc-list output and extract card information."""
    cards = []
    current_card = None
    device_name = None

    for line in output.split('\n'):
        # Extract device name
        if 'NFC device:' in line and 'opened' in line:
            match = re.search(r'NFC device: (.+?) opened', line)
            if match:
                device_name = match.group(1)

        # Start of a new target
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

        # Extract card details
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

    # Remove duplicates (same UID from different devices)
    seen_uids = set()
    unique_cards = []
    for card in cards:
        if card['uid'] and card['uid'] not in seen_uids:
            seen_uids.add(card['uid'])
            unique_cards.append(card)

    return unique_cards


def run_nfc_list() -> dict:
    """Run nfc-list command and return parsed results."""
    try:
        result = subprocess.run(
            ['nfc-list'],
            capture_output=True,
            text=True,
            timeout=10
        )

        output = result.stdout + result.stderr
        cards = parse_nfc_list_output(output)

        return {
            'success': True,
            'cards': cards,
            'raw_output': output
        }
    except subprocess.TimeoutExpired:
        return {
            'success': False,
            'error': 'Command timed out',
            'cards': []
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'cards': []
        }


def create_ndef_file(ndef_type: str, content: str) -> str:
    """Create NDEF file and return path."""
    if ndef_type == 'url':
        record = ndef.UriRecord(content)
    elif ndef_type == 'text':
        record = ndef.TextRecord(content)
    else:
        raise ValueError(f"Unknown NDEF type: {ndef_type}")

    # Create temp file
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
    global emulation_process, ndef_file_path

    # Check if already emulating
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
        # Create NDEF file
        ndef_file_path = create_ndef_file(ndef_type, content)

        # Start emulation
        emulation_process = subprocess.Popen(
            ['nfc-emulate-forum-tag4', ndef_file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid
        )

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
        # Kill process group
        os.killpg(os.getpgid(emulation_process.pid), signal.SIGTERM)
        emulation_process.wait(timeout=5)
    except Exception:
        try:
            emulation_process.kill()
        except Exception:
            pass

    emulation_process = None

    # Clean up NDEF file
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
