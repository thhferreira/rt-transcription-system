#!/bin/bash

# RT Transcription System Startup Script
# Allows selection between single-process and multi-process architectures

echo "======================================"
echo "RT Transcription System"
echo "======================================"
echo ""
echo "Select architecture:"
echo "1) Multi-Process with tmux (Recommended)"
echo "2) Single-Process (Original)"
echo "3) Multi-Process without tmux"
echo ""

read -p "Enter choice [1-3]: " choice

case $choice in
    1)
        echo "Starting Multi-Process architecture with tmux..."
        
        # Check if tmux is installed
        if ! command -v tmux &> /dev/null; then
            echo "Error: tmux is not installed."
            echo "Install with: brew install tmux (macOS) or apt-get install tmux (Linux)"
            exit 1
        fi
        
        # Check if session already exists
        if tmux has-session -t rt_transcription 2>/dev/null; then
            echo "Existing tmux session found. Attaching..."
            tmux attach-session -t rt_transcription
        else
            echo "Creating new tmux session and starting coordinator..."
            python3 coordinator.py &
            sleep 2
            tmux attach-session -t rt_transcription
        fi
        ;;
        
    2)
        echo "Starting Single-Process architecture..."
        python3 rt_transcribe.py
        ;;
        
    3)
        echo "Starting Multi-Process architecture without tmux..."
        # Modify config to disable tmux
        python3 -c "
import yaml
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)
config.setdefault('architecture', {})['use_tmux'] = False
with open('config.yaml', 'w') as f:
    yaml.dump(config, f, default_flow_style=False)
print('Config updated: tmux disabled')
"
        python3 coordinator.py
        ;;
        
    *)
        echo "Invalid choice. Exiting."
        exit 1
        ;;
esac