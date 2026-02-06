#!/usr/bin/env python3
"""
Web interface for PN532 NFC reader — direct serial communication.
"""

import threading
from collections import deque

import ndef
from flask import Flask, render_template, jsonify, request
from pn532 import PN532

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


@app.route('/')
def index():
    """Render the main page."""
    return render_template('index.html')


@app.route('/api/scan')
def scan():
    """Scan for NFC cards."""
    result = pn532_reader.scan_type_a()
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

    if not content:
        return jsonify({
            'success': False,
            'error': 'Content is required'
        })

    try:
        ndef_bytes = build_ndef_bytes(ndef_type, content)

        # Clear log buffer
        with log_lock:
            log_buffer.clear()

        emulation_stop_event = threading.Event()

        emulation_thread = threading.Thread(
            target=pn532_reader.emulate_tag,
            args=(ndef_bytes, emulation_stop_event, log_buffer),
            daemon=True,
        )
        emulation_thread.start()

        return jsonify({
            'success': True,
            'message': f'Emulating Type 4 tag with {ndef_type}: {content}',
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
