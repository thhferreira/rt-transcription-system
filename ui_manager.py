#!/usr/bin/env python3
"""
UI Manager - Subprocess for terminal window and display management
Handles tmux panes, terminal output, and user interface
"""

import logging
import time
import subprocess
import json
from datetime import datetime
from collections import deque
import sys
import os

class UIManager:
    def __init__(self, config, queues, shared_state, shutdown_event, tmux_session=None):
        self.config = config
        self.queues = queues
        self.shared_state = shared_state
        self.shutdown_event = shutdown_event
        self.tmux_session = tmux_session
        
        self.setup_logging()
        
        self.notes_buffer = deque(maxlen=10)
        self.status_buffer = deque(maxlen=20)
        self.transcript_buffer = deque(maxlen=50)
        
        self.use_tmux = config.get('architecture', {}).get('use_tmux', True) and tmux_session
        self.display_mode = config.get('output', {}).get('display_format', 'clean')
        
    def setup_logging(self):
        self.logger = logging.getLogger('UIManager')
        self.logger.setLevel(logging.INFO)
        
    def send_to_tmux_pane(self, pane_idx, content):
        if not self.use_tmux or not self.tmux_session:
            return False
            
        try:
            escaped_content = content.replace("'", "'\\''")
            
            cmd = [
                'tmux', 'send-keys', '-t', f'{self.tmux_session}:0.{pane_idx}',
                'C-c', 'Enter'
            ]
            subprocess.run(cmd, capture_output=True)
            
            cmd = [
                'tmux', 'send-keys', '-t', f'{self.tmux_session}:0.{pane_idx}',
                f"clear && echo '{escaped_content}'", 'Enter'
            ]
            subprocess.run(cmd, capture_output=True)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to send to tmux pane {pane_idx}: {e}")
            return False
            
    def display_in_new_terminal(self, content):
        if sys.platform == 'darwin':
            try:
                escaped = content.replace('\\', '\\\\')
                escaped = escaped.replace('"', '\\"')
                escaped = escaped.replace('$', '\\$')
                escaped = escaped.replace('`', '\\`')
                escaped = escaped.replace('\n', '\\n')
                
                script = f'''
                tell application "Terminal"
                    if not (exists window 1) then reopen
                    activate
                    do script "clear && echo \\"{escaped}\\" && echo \\"\\\\n--- Press Ctrl+C to close ---\\" && read -r -d ''"
                end tell'''
                
                subprocess.run(['osascript', '-e', script], check=True, timeout=5)
                return True
                
            except Exception as e:
                self.logger.error(f"Failed to open new terminal: {e}")
                return False
                
        elif sys.platform.startswith('linux'):
            terminals = ['gnome-terminal', 'konsole', 'xterm', 'terminator']
            
            for terminal in terminals:
                try:
                    if terminal == 'gnome-terminal':
                        subprocess.Popen([
                            terminal, '--', 'bash', '-c', 
                            f'echo "{content}"; echo "Press Enter to close"; read'
                        ])
                    else:
                        subprocess.Popen([
                            terminal, '-e', 'bash', '-c',
                            f'echo "{content}"; echo "Press Enter to close"; read'
                        ])
                    return True
                except FileNotFoundError:
                    continue
                    
        return False
        
    def format_status_display(self):
        lines = []
        lines.append("="*60)
        lines.append("SYSTEM STATUS")
        lines.append("="*60)
        
        lines.append(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Memory Usage: {self.shared_state.get('memory_usage', 0):.1f} MB")
        lines.append(f"Total Segments: {self.shared_state.get('total_segments', 0)}")
        
        last_processed = self.shared_state.get('last_processed', 0)
        if last_processed > 0:
            last_time = datetime.fromtimestamp(last_processed).strftime('%H:%M:%S')
            lines.append(f"Last Processed: {last_time}")
            
        lines.append("-"*60)
        
        if self.status_buffer:
            latest_status = self.status_buffer[-1]
            lines.append(f"CPU: {latest_status.get('cpu_percent', 0):.1f}%")
            lines.append(f"Total Memory: {latest_status.get('memory_mb', 0):.1f} MB")
            lines.append("")
            lines.append("Process Status:")
            
            for proc_name, proc_info in latest_status.get('processes_status', {}).items():
                status = "✓ Running" if proc_info['alive'] else "✗ Stopped"
                lines.append(f"  {proc_name}: {status}")
                
        return "\n".join(lines)
        
    def format_notes_display(self):
        lines = []
        lines.append("="*60)
        lines.append("GENERATED NOTES")
        lines.append("="*60)
        
        if not self.notes_buffer:
            lines.append("No notes generated yet...")
        else:
            for note_data in self.notes_buffer:
                lines.append(f"\n[{note_data['timestamp']}]")
                lines.append(f"Depth: {note_data['depth_level']} | Segments: {note_data['segment_count']}")
                lines.append("-"*40)
                lines.append(note_data['notes'])
                lines.append("")
                
        return "\n".join(lines)
        
    def format_transcript_display(self):
        lines = []
        lines.append("="*60)
        lines.append("RECENT TRANSCRIPTS")
        lines.append("="*60)
        
        if not self.transcript_buffer:
            lines.append("No transcript segments yet...")
        else:
            for segment in list(self.transcript_buffer)[-20:]:
                time_str = datetime.fromtimestamp(segment['start']).strftime('%H:%M:%S')
                lines.append(f"[{time_str}] {segment['text']}")
                
        return "\n".join(lines)
        
    def update_displays(self):
        if self.use_tmux:
            self.send_to_tmux_pane(1, self.format_transcript_display())
            self.send_to_tmux_pane(2, self.format_notes_display())
            self.send_to_tmux_pane(3, self.format_status_display())
        else:
            print("\033[2J\033[H")
            print(self.format_status_display())
            print("\n")
            print(self.format_transcript_display()[:500])
            print("\n")
            print(self.format_notes_display()[:1000])
            
    def handle_ui_command(self, command):
        cmd_type = command.get('command')
        
        if cmd_type == 'cleanup_memory':
            self.notes_buffer.clear()
            self.status_buffer.clear()
            self.transcript_buffer = deque(maxlen=50)
            self.logger.info("UI memory cleaned up")
            
        elif cmd_type == 'refresh':
            self.update_displays()
            
        elif cmd_type == 'show_notes':
            notes = command.get('notes')
            if notes:
                if self.config['output'].get('new_terminal', True):
                    self.display_in_new_terminal(notes)
                else:
                    print(notes)
                    
    def run(self):
        self.logger.info("UI Manager started")
        self.logger.info(f"Using tmux: {self.use_tmux}")
        
        last_update = time.time()
        update_interval = 2
        
        while not self.shutdown_event.is_set():
            try:
                try:
                    notes_data = self.queues['notes'].get_nowait()
                    self.notes_buffer.append(notes_data)
                    
                    if self.config['output'].get('new_terminal', True):
                        self.display_in_new_terminal(notes_data['notes'])
                        
                    self.logger.info("Displayed new notes")
                except:
                    pass
                    
                try:
                    status_data = self.queues['status'].get_nowait()
                    self.status_buffer.append(status_data)
                except:
                    pass
                    
                try:
                    transcript_data = self.queues['transcript'].get_nowait()
                    if 'segments' in transcript_data:
                        self.transcript_buffer.extend(transcript_data['segments'])
                except:
                    pass
                    
                try:
                    ui_command = self.queues['ui_commands'].get_nowait()
                    self.handle_ui_command(ui_command)
                except:
                    pass
                    
                if time.time() - last_update > update_interval:
                    self.update_displays()
                    last_update = time.time()
                    
                time.sleep(0.1)
                
            except Exception as e:
                self.logger.error(f"Error in UI loop: {e}", exc_info=True)
                time.sleep(1)
                
        self.logger.info("UI Manager shutting down")
        
if __name__ == "__main__":
    import yaml
    import multiprocessing as mp
    
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)
        
    queues = {
        'transcript': mp.Queue(),
        'notes': mp.Queue(),
        'ui_commands': mp.Queue(),
        'status': mp.Queue()
    }
    
    shared_state = mp.Manager().dict()
    shutdown_event = mp.Event()
    
    ui = UIManager(config, queues, shared_state, shutdown_event, None)
    
    try:
        ui.run()
    except KeyboardInterrupt:
        shutdown_event.set()
        print("UI Manager stopped")