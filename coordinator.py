#!/usr/bin/env python3
"""
Multi-Process Coordinator for RT Transcription System
Manages separate processes for monitoring, processing, and UI
"""

import multiprocessing as mp
import subprocess
import threading
import signal
import sys
import time
import json
import os
import yaml
import psutil
from datetime import datetime
from collections import deque
from queue import Queue, Empty
import logging

# Module-level functions for multiprocessing
def run_transcript_monitor(config, output_queue, shared_state, shutdown_event):
    from transcript_monitor import TranscriptMonitor
    monitor = TranscriptMonitor(config, output_queue, shared_state, shutdown_event)
    monitor.run()

def run_note_generator(config, input_queue, output_queue, shared_state, shutdown_event):
    from note_generator import NoteGenerator
    generator = NoteGenerator(config, input_queue, output_queue, shared_state, shutdown_event)
    generator.run()

def run_ui_manager(config, queues, shared_state, shutdown_event, tmux_session):
    from ui_manager import UIManager
    ui = UIManager(config, queues, shared_state, shutdown_event, tmux_session)
    ui.run()

class SystemCoordinator:
    def __init__(self, config_path="config.yaml"):
        self.config = self.load_config(config_path)
        self.setup_logging()
        
        self.processes = {}
        self.queues = {
            'transcript': mp.Queue(),
            'notes': mp.Queue(), 
            'ui_commands': mp.Queue(),
            'status': mp.Queue()
        }
        
        self.shared_state = mp.Manager().dict()
        self.shared_state['running'] = True
        self.shared_state['memory_usage'] = 0
        self.shared_state['last_processed'] = 0
        self.shared_state['total_segments'] = 0
        
        self.shutdown_event = mp.Event()
        self.tmux_session = None
        
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
    def load_config(self, config_path):
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        config.setdefault('architecture', {})
        config['architecture'].setdefault('use_tmux', True)
        config['architecture'].setdefault('max_memory_mb', 500)
        config['architecture'].setdefault('process_restart_delay', 5)
        config['architecture'].setdefault('health_check_interval', 30)
        
        return config
        
    def setup_logging(self):
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(processName)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(f"{log_dir}/coordinator_{datetime.now():%Y%m%d_%H%M%S}.log"),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def setup_tmux_session(self):
        if not self.config['architecture'].get('use_tmux', True):
            return False
            
        session_name = "rt_transcription"
        
        try:
            subprocess.run(['tmux', 'has-session', '-t', session_name], 
                         capture_output=True, check=True)
            self.logger.info(f"Attaching to existing tmux session: {session_name}")
        except subprocess.CalledProcessError:
            self.logger.info(f"Creating new tmux session: {session_name}")
            subprocess.run([
                'tmux', 'new-session', '-d', '-s', session_name, '-n', 'coordinator'
            ])
            
            subprocess.run([
                'tmux', 'split-window', '-t', f'{session_name}:0', '-h'
            ])
            subprocess.run([
                'tmux', 'split-window', '-t', f'{session_name}:0.0', '-v'
            ])
            subprocess.run([
                'tmux', 'split-window', '-t', f'{session_name}:0.2', '-v'
            ])
            
            pane_titles = ['Coordinator', 'Transcript Monitor', 'Note Generator', 'System Status']
            for i, title in enumerate(pane_titles):
                subprocess.run([
                    'tmux', 'select-pane', '-t', f'{session_name}:0.{i}', 
                    '-T', title
                ])
        
        self.tmux_session = session_name
        return True
        
    def send_to_tmux_pane(self, pane_idx, command):
        if not self.tmux_session:
            return
            
        try:
            subprocess.run([
                'tmux', 'send-keys', '-t', f'{self.tmux_session}:0.{pane_idx}',
                command, 'Enter'
            ])
        except Exception as e:
            self.logger.error(f"Failed to send command to tmux pane {pane_idx}: {e}")
            
    def start_transcript_monitor(self):
        process = mp.Process(
            target=run_transcript_monitor, 
            args=(self.config, self.queues['transcript'], self.shared_state, self.shutdown_event),
            name="TranscriptMonitor"
        )
        process.start()
        self.processes['transcript_monitor'] = process
        self.logger.info("Started Transcript Monitor process")
        
    def start_note_generator(self):
        process = mp.Process(
            target=run_note_generator,
            args=(self.config, self.queues['transcript'], self.queues['notes'], 
                  self.shared_state, self.shutdown_event),
            name="NoteGenerator"
        )
        process.start()
        self.processes['note_generator'] = process
        self.logger.info("Started Note Generator process")
        
    def start_ui_manager(self):
        process = mp.Process(
            target=run_ui_manager,
            args=(self.config, self.queues, self.shared_state, 
                  self.shutdown_event, self.tmux_session),
            name="UIManager"
        )
        process.start()
        self.processes['ui_manager'] = process
        self.logger.info("Started UI Manager process")
        
    def monitor_system_health(self):
        while not self.shutdown_event.is_set():
            try:
                current_process = psutil.Process()
                memory_info = current_process.memory_info()
                memory_mb = memory_info.rss / 1024 / 1024
                
                self.shared_state['memory_usage'] = memory_mb
                
                cpu_percent = current_process.cpu_percent(interval=1)
                
                children = current_process.children(recursive=True)
                total_memory = memory_mb
                for child in children:
                    try:
                        child_memory = child.memory_info().rss / 1024 / 1024
                        total_memory += child_memory
                    except:
                        pass
                
                status_msg = {
                    'timestamp': datetime.now().isoformat(),
                    'cpu_percent': cpu_percent,
                    'memory_mb': total_memory,
                    'num_processes': len(self.processes),
                    'processes_status': {}
                }
                
                for name, process in self.processes.items():
                    status_msg['processes_status'][name] = {
                        'alive': process.is_alive(),
                        'pid': process.pid if process.is_alive() else None
                    }
                
                try:
                    self.queues['status'].put_nowait(status_msg)
                except:
                    pass
                
                if total_memory > self.config['architecture']['max_memory_mb']:
                    self.logger.warning(f"Memory usage ({total_memory:.1f}MB) exceeds limit ({self.config['architecture']['max_memory_mb']}MB)")
                    self.trigger_memory_cleanup()
                
                for name, process in list(self.processes.items()):
                    if not process.is_alive():
                        self.logger.error(f"Process {name} died unexpectedly")
                        self.restart_process(name)
                
                time.sleep(self.config['architecture']['health_check_interval'])
                
            except Exception as e:
                self.logger.error(f"Health monitoring error: {e}")
                time.sleep(5)
                
    def trigger_memory_cleanup(self):
        self.logger.info("Triggering memory cleanup across all processes")
        
        try:
            self.queues['ui_commands'].put_nowait({'command': 'cleanup_memory'})
        except:
            pass
            
    def restart_process(self, process_name):
        self.logger.info(f"Attempting to restart {process_name}")
        
        old_process = self.processes.get(process_name)
        if old_process and old_process.is_alive():
            old_process.terminate()
            old_process.join(timeout=5)
            if old_process.is_alive():
                old_process.kill()
                
        time.sleep(self.config['architecture']['process_restart_delay'])
        
        if process_name == 'transcript_monitor':
            self.start_transcript_monitor()
        elif process_name == 'note_generator':
            self.start_note_generator()
        elif process_name == 'ui_manager':
            self.start_ui_manager()
            
    def signal_handler(self, signum, frame):
        self.logger.info(f"Received signal {signum}, initiating shutdown...")
        self.shutdown()
        
    def shutdown(self):
        self.logger.info("Starting graceful shutdown...")
        self.shutdown_event.set()
        
        for name, process in self.processes.items():
            self.logger.info(f"Terminating {name}...")
            process.terminate()
            
        for name, process in self.processes.items():
            process.join(timeout=5)
            if process.is_alive():
                self.logger.warning(f"Force killing {name}")
                process.kill()
                
        self.logger.info("All processes terminated")
        
        if self.tmux_session:
            self.logger.info(f"Tmux session '{self.tmux_session}' remains active for review")
            
        sys.exit(0)
        
    def run(self):
        self.logger.info("="*60)
        self.logger.info("RT Transcription System - Multi-Process Architecture")
        self.logger.info("="*60)
        
        if self.config['architecture'].get('use_tmux', True):
            if not self.setup_tmux_session():
                self.logger.warning("tmux not available, falling back to standard output")
        
        self.start_transcript_monitor()
        time.sleep(1)
        
        self.start_note_generator()
        time.sleep(1)
        
        self.start_ui_manager()
        time.sleep(1)
        
        health_thread = threading.Thread(target=self.monitor_system_health)
        health_thread.daemon = True
        health_thread.start()
        
        self.logger.info("All subsystems started. System is running.")
        self.logger.info(f"Memory limit: {self.config['architecture']['max_memory_mb']}MB")
        self.logger.info("Press Ctrl+C to shutdown gracefully")
        
        try:
            while not self.shutdown_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown()

def main():
    coordinator = SystemCoordinator()
    coordinator.run()

if __name__ == "__main__":
    main()