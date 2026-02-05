#!/bin/bash
cd "$(dirname "$0")"

# Create venv if not exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

# Check if already running
if [ -f ".pid" ]; then
    PID=$(cat .pid)
    if ps -p $PID > /dev/null 2>&1; then
        echo "Server already running (PID: $PID)"
        echo "http://localhost:5001"
        exit 0
    fi
fi

# Start server
echo "Starting PN532 NFC Reader..."
nohup python app.py > server.log 2>&1 &
echo $! > .pid
sleep 1

if ps -p $(cat .pid) > /dev/null 2>&1; then
    echo "Server started (PID: $(cat .pid))"
    echo "http://localhost:5001"
else
    echo "Failed to start server. Check server.log"
    exit 1
fi
