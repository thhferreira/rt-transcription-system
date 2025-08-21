#!/usr/bin/env python3
"""
Transcript Monitor - Subprocess for watching transcript files
Memory-efficient streaming of transcript segments
"""

import os
import json
import time
import logging
from datetime import datetime
from collections import deque
from pathlib import Path
import gc

class TranscriptMonitor:
    def __init__(self, config, output_queue, shared_state, shutdown_event):
        self.config = config
        self.output_queue = output_queue
        self.shared_state = shared_state
        self.shutdown_event = shutdown_event
        
        self.setup_logging()
        
        self.transcript_path = Path(self.config['files']['transcript_path'])
        self.processed_log = Path(self.config['files']['processed_log'])
        self.last_processed_time = self.load_last_processed_time()
        
        self.max_buffer_size = config.get('architecture', {}).get('transcript_buffer_size', 100)
        self.recent_segments = deque(maxlen=self.max_buffer_size)
        
        self.check_interval = self.config['monitoring']['interval_minutes'] * 60
        
    def setup_logging(self):
        self.logger = logging.getLogger('TranscriptMonitor')
        self.logger.setLevel(logging.INFO)
        
    def load_last_processed_time(self):
        if self.processed_log.exists():
            try:
                with open(self.processed_log, 'r') as f:
                    content = f.read().strip()
                    if content:
                        return float(content)
            except (ValueError, IOError) as e:
                self.logger.warning(f"Could not load last processed time: {e}")
        return 0.0
        
    def save_last_processed_time(self, timestamp):
        try:
            with open(self.processed_log, 'w') as f:
                f.write(str(timestamp))
        except IOError as e:
            self.logger.error(f"Failed to save last processed time: {e}")
            
    def get_latest_session_path(self):
        if not self.transcript_path.is_dir():
            return None
            
        session_dirs = [
            d for d in self.transcript_path.iterdir()
            if d.is_dir() and d.name.startswith("session_")
        ]
        
        if not session_dirs:
            return None
            
        return max(session_dirs, key=lambda d: d.name)
        
    def stream_transcript_segments(self, json_path):
        """
        Memory-efficient streaming of transcript segments
        Uses generator to avoid loading entire file into memory
        """
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                file_size = os.path.getsize(json_path)
                
                if file_size > 10 * 1024 * 1024:  # 10MB threshold
                    self.logger.info(f"Large transcript file ({file_size/1024/1024:.1f}MB), using streaming mode")
                    
                    decoder = json.JSONDecoder()
                    buffer = ''
                    
                    for chunk in iter(lambda: f.read(4096), ''):
                        buffer += chunk
                        buffer = buffer.lstrip()
                        
                        if not buffer:
                            continue
                            
                        if buffer[0] == '[':
                            buffer = buffer[1:].lstrip()
                            
                        while buffer:
                            try:
                                if buffer[0] == ']':
                                    break
                                    
                                obj, idx = decoder.raw_decode(buffer)
                                yield obj
                                
                                buffer = buffer[idx:].lstrip()
                                if buffer and buffer[0] == ',':
                                    buffer = buffer[1:].lstrip()
                                    
                            except json.JSONDecodeError:
                                break
                else:
                    data = json.load(f)
                    for segment in data:
                        yield segment
                        
        except (IOError, json.JSONDecodeError) as e:
            self.logger.error(f"Error reading transcript file {json_path}: {e}")
            return
            
    def process_new_segments(self):
        latest_session = self.get_latest_session_path()
        if not latest_session:
            return []
            
        transcript_files = list(latest_session.glob("transcript_chunks_*.json"))
        if not transcript_files:
            return []
            
        transcript_file = transcript_files[0]
        
        new_segments = []
        segments_processed = 0
        
        for segment in self.stream_transcript_segments(transcript_file):
            segments_processed += 1
            
            if segments_processed % 100 == 0:
                gc.collect()
                
            timestamp_str = segment.get('timestamp')
            raw_text = segment.get('raw_transcript')
            
            if not timestamp_str or raw_text is None:
                continue
                
            try:
                dt_object = datetime.fromisoformat(timestamp_str)
                segment_time = dt_object.timestamp()
            except ValueError:
                self.logger.warning(f"Invalid timestamp: {timestamp_str}")
                continue
                
            if segment_time > self.last_processed_time:
                segment_data = {
                    'start': segment_time,
                    'text': raw_text,
                    'chunk_id': segment.get('chunk_id'),
                    'timestamp': timestamp_str
                }
                
                new_segments.append(segment_data)
                self.recent_segments.append(segment_data)
                
                if len(new_segments) >= 50:
                    self.flush_segments(new_segments)
                    new_segments = []
                    
        if new_segments:
            self.flush_segments(new_segments)
            
        self.shared_state['total_segments'] = len(self.recent_segments)
        
        return segments_processed
        
    def flush_segments(self, segments):
        if not segments:
            return
            
        batch = {
            'segments': segments,
            'batch_time': datetime.now().isoformat(),
            'count': len(segments)
        }
        
        try:
            self.output_queue.put(batch, timeout=5)
            
            if segments:
                latest_time = max(s['start'] for s in segments)
                self.last_processed_time = latest_time
                self.save_last_processed_time(latest_time)
                self.shared_state['last_processed'] = latest_time
                
            self.logger.info(f"Flushed {len(segments)} segments to processing queue")
            
        except Exception as e:
            self.logger.error(f"Failed to flush segments: {e}")
            
    def cleanup_memory(self):
        self.recent_segments.clear()
        gc.collect()
        self.logger.info("Memory cleanup completed")
        
    def run(self):
        self.logger.info("Transcript Monitor started")
        self.logger.info(f"Monitoring: {self.transcript_path}")
        self.logger.info(f"Buffer size: {self.max_buffer_size} segments")
        
        last_cleanup = time.time()
        cleanup_interval = 300  # 5 minutes
        
        while not self.shutdown_event.is_set():
            try:
                self.logger.debug("Checking for new transcript segments...")
                segments_count = self.process_new_segments()
                
                if segments_count > 0:
                    self.logger.info(f"Processed {segments_count} transcript segments")
                    
                if time.time() - last_cleanup > cleanup_interval:
                    self.cleanup_memory()
                    last_cleanup = time.time()
                    
                self.shutdown_event.wait(timeout=self.check_interval)
                
            except Exception as e:
                self.logger.error(f"Error in monitor loop: {e}", exc_info=True)
                time.sleep(5)
                
        self.logger.info("Transcript Monitor shutting down")
        
if __name__ == "__main__":
    import yaml
    import multiprocessing as mp
    
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)
        
    queue = mp.Queue()
    shared_state = mp.Manager().dict()
    shutdown_event = mp.Event()
    
    monitor = TranscriptMonitor(config, queue, shared_state, shutdown_event)
    
    try:
        monitor.run()
    except KeyboardInterrupt:
        shutdown_event.set()
        print("Monitor stopped")