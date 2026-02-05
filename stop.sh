#!/bin/bash
cd "$(dirname "$0")"

if [ -f ".pid" ]; then
    PID=$(cat .pid)
    if ps -p $PID > /dev/null 2>&1; then
        kill $PID
        rm .pid
        echo "Server stopped (PID: $PID)"
    else
        rm .pid
        echo "Server not running"
    fi
else
    echo "No PID file found"
fi
