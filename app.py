#!/usr/bin/env python3
"""
Web interface for PN532 NFC reader — direct serial communication.
"""

import threading
from collections import deque

import ndef
from flask import Flask, render_template, jsonify, request
from pn532 import PN532, Type4TagEmulator, VaultTagEmulator

app = Flask(__name__)

# PN532 direct serial reader
pn532_reader = PN532()

# Emulation state
emulation_thread = None
emulation_stop_event = None
log_buffer = deque(maxlen=500)
log_lock = threading.Lock()


def build_ndef_bytes(ndef_type: str, content: str) -> bytes:
    """Encode an NDEF message and return raw bytes."""
    if ndef_type == 'url':
        record = ndef.UriRecord(content)
    elif ndef_type == 'text':
        record = ndef.TextRecord(content)
    else:
        raise ValueError(f"Unknown NDEF type: {ndef_type}")
    return b''.join(ndef.message_encoder([record]))


def parse_custom_content(content: str, fmt: str) -> bytes:
    """Parse user input into bytes for the Vault protocol buffer."""
    if fmt == 'hex':
        # Accept hex string like "48 65 6C 6C 6F" or "48656C6C6F"
        cleaned = content.replace(' ', '').replace('\n', '').replace('\r', '')
        return bytes.fromhex(cleaned)
    # Default: UTF-8 text
    return content.encode('utf-8')


@app.route('/')
def index():
    """Render the main page."""
    return render_template('index.html')


@app.route('/api/scan')
def scan():
    """Scan for NFC cards."""
    result = pn532_reader.scan_type_a()
    return jsonify(result)


@app.route('/api/read-vault')
def read_vault():
    """Read data from a card using the Vault APDU protocol."""
    offset = request.args.get('offset', 0, type=int)
    length = request.args.get('length', 64, type=int)
    result = pn532_reader.read_vault_tag(read_offset=offset, read_length=length)
    return jsonify(result)


@app.route('/api/read-ndef')
def read_ndef():
    """Read NDEF message from a Type 4 Tag."""
    result = pn532_reader.read_ndef_tag()
    return jsonify(result)


@app.route('/api/write-vault', methods=['POST'])
def write_vault():
    """Write data to a card using the Vault APDU protocol."""
    data = request.get_json()
    offset = data.get('offset', 0)
    content = data.get('content', '')
    fmt = data.get('format', 'text')

    if not content:
        return jsonify({'success': False, 'error': 'Content is required'})

    try:
        data_bytes = parse_custom_content(content, fmt)
    except ValueError as e:
        return jsonify({'success': False, 'error': f'Invalid input: {e}'})

    result = pn532_reader.write_vault_tag(write_offset=offset, data_bytes=data_bytes)
    return jsonify(result)


@app.route('/api/write-ndef', methods=['POST'])
def write_ndef():
    """Write an NDEF message to a Type 4 Tag."""
    data = request.get_json()
    ndef_type = data.get('type', 'text')
    content = data.get('content', '')

    if not content:
        return jsonify({'success': False, 'error': 'Content is required'})

    try:
        ndef_msg_bytes = build_ndef_bytes(ndef_type, content)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)})

    result = pn532_reader.write_ndef_tag(ndef_msg_bytes)
    return jsonify(result)


@app.route('/api/emulate/start', methods=['POST'])
def emulate_start():
    """Start Type 4 tag emulation."""
    global emulation_thread, emulation_stop_event

    if emulation_thread and emulation_thread.is_alive():
        return jsonify({
            'success': False,
            'error': 'Emulation already running'
        })

    data = request.get_json()
    ndef_type = data.get('type', 'text')
    content = data.get('content', '')
    input_format = data.get('format', 'text')

    if not content and ndef_type != 'vault':
        return jsonify({
            'success': False,
            'error': 'Content is required'
        })

    try:
        if ndef_type in ('url', 'text'):
            ndef_bytes = build_ndef_bytes(ndef_type, content)
            emulator = Type4TagEmulator(ndef_bytes)
            message = f'Emulating Type 4 tag with {ndef_type}: {content}'
        elif ndef_type == 'vault':
            initial_data = parse_custom_content(content, input_format) if content else b""
            emulator = VaultTagEmulator(initial_data)
            message = f'Emulating Vault protocol (buffer: {len(initial_data)} bytes)'
        else:
            return jsonify({
                'success': False,
                'error': f'Unknown type: {ndef_type}'
            })

        # Clear log buffer
        with log_lock:
            log_buffer.clear()

        emulation_stop_event = threading.Event()

        emulation_thread = threading.Thread(
            target=pn532_reader.emulate_tag,
            args=(emulator, emulation_stop_event, log_buffer),
            daemon=True,
        )
        emulation_thread.start()

        return jsonify({
            'success': True,
            'message': message,
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/emulate/stop', methods=['POST'])
def emulate_stop():
    """Stop Type 4 tag emulation."""
    global emulation_thread, emulation_stop_event

    if not emulation_thread or not emulation_thread.is_alive():
        return jsonify({
            'success': False,
            'error': 'No emulation running'
        })

    emulation_stop_event.set()
    emulation_thread.join(timeout=5)
    emulation_thread = None
    emulation_stop_event = None

    return jsonify({
        'success': True,
        'message': 'Emulation stopped'
    })


@app.route('/api/emulate/status')
def emulate_status():
    """Get emulation status."""
    if emulation_thread and emulation_thread.is_alive():
        return jsonify({'running': True})

    return jsonify({'running': False})


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
