#!/usr/bin/env python3
"""
Web interface for PN532 NFC reader using libnfc
"""

import subprocess
import re
from flask import Flask, render_template, jsonify

app = Flask(__name__)


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


@app.route('/')
def index():
    """Render the main page."""
    return render_template('index.html')


@app.route('/api/scan')
def scan():
    """Scan for NFC cards."""
    result = run_nfc_list()
    return jsonify(result)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
