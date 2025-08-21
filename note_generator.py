#!/usr/bin/env python3
"""
Note Generator - Subprocess for AI-powered note generation
Processes transcript segments and generates research notes
"""

import logging
import time
import gc
from datetime import datetime
from collections import deque
from openai import OpenAI
import httpx

class NoteGenerator:
    def __init__(self, config, input_queue, output_queue, shared_state, shutdown_event):
        self.config = config
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.shared_state = shared_state
        self.shutdown_event = shutdown_event
        
        self.setup_logging()
        self.setup_ai_client()
        
        self.processing_buffer = deque(maxlen=50)
        self.batch_timeout = 30
        self.min_batch_size = 5
        
    def setup_logging(self):
        self.logger = logging.getLogger('NoteGenerator')
        self.logger.setLevel(logging.INFO)
        
    def setup_ai_client(self):
        deepseek_cfg = self.config.get('deepseek', {})
        api_key = deepseek_cfg.get('api_key')
        
        if not api_key or api_key == "YOUR_DEEPSEEK_API_KEY_HERE":
            self.logger.warning("DeepSeek API key not configured - running in demo mode")
            self.client = None
            self.model_name = "demo-mode"
            self.max_tokens = 1500
            return
            
        self.client = OpenAI(
            api_key=api_key,
            base_url=deepseek_cfg.get('base_url', "https://api.deepseek.com"),
            max_retries=deepseek_cfg.get('max_retries', 3),
            timeout=httpx.Timeout(
                deepseek_cfg.get('timeout_connect', 15.0),
                read=deepseek_cfg.get('timeout_read', 60.0),
                write=deepseek_cfg.get('timeout_write', 10.0),
                pool=deepseek_cfg.get('timeout_pool', 5.0)
            )
        )
        
        self.model_name = deepseek_cfg.get('model', 'deepseek-chat')
        self.max_tokens = deepseek_cfg.get('max_tokens_completion', 1500)
        
        self.logger.info(f"AI client configured with model: {self.model_name}")
        
    def format_segments_for_ai(self, segments):
        formatted_text = ""
        
        for segment in segments:
            time_str = datetime.fromtimestamp(segment['start']).strftime('%H:%M:%S')
            formatted_text += f"[{time_str}] {segment['text']}\n"
            
        return formatted_text.strip()
        
    def get_prompt_for_depth(self, depth_level):
        prompts = {
            'minimal': """
            You are an assistant that creates minimal factual notes from a transcript.
            Focus on WHAT was said, not WHY. Include:
            - Direct statements made
            - Actions mentioned
            - Specific details shared
            Format: [Time] TYPE: Content
            """,
            
            'standard': """
            You are an assistant that creates factual notes with minimal context.
            Focus on WHAT happened:
            - What participant said
            - What participant did
            - Specific details mentioned
            - Light context only when essential
            Format: [Time] TYPE: Content with brief context
            """,
            
            'detailed': """
            You are an assistant that creates detailed factual notes.
            Include:
            - Complete statements with context
            - All actions and behaviors
            - Specific details, numbers, brands
            - Sequence of events
            Format: [Time] TYPE: Comprehensive description
            """,
            
            'comprehensive': """
            You are an assistant that creates comprehensive documentation.
            Capture everything:
            - Every significant statement
            - All behaviors and reactions
            - Complete timeline
            - Full verbatim quotes
            - All specific details
            Format: [Time] TYPE: Complete record with quotes
            """
        }
        
        return prompts.get(depth_level, prompts['standard'])
        
    def generate_notes(self, segments):
        if not segments:
            return None
            
        depth_level = self.config['note_taking']['depth_level']
        formatted_text = self.format_segments_for_ai(segments)
        
        if not formatted_text:
            return None
            
        # Demo mode if no client
        if not self.client:
            self.logger.info(f"Demo mode: Would generate {depth_level} notes for {len(segments)} segments")
            demo_notes = f"[DEMO MODE - No API Key]\n"
            demo_notes += f"Depth: {depth_level}\n"
            demo_notes += f"Segments: {len(segments)}\n"
            demo_notes += f"-"*40 + "\n"
            for seg in segments[:3]:  # Show first 3 segments
                time_str = datetime.fromtimestamp(seg['start']).strftime('%H:%M:%S')
                demo_notes += f"[{time_str}] {seg['text'][:50]}...\n"
            demo_notes += f"\n(Configure DeepSeek API key in config.yaml for actual notes)"
            return demo_notes
            
        system_prompt = self.get_prompt_for_depth(depth_level)
        
        messages = [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": formatted_text}
        ]
        
        try:
            self.logger.info(f"Generating {depth_level} notes for {len(segments)} segments")
            
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                stream=False,
                max_tokens=self.max_tokens
            )
            
            notes = response.choices[0].message.content
            
            if not notes:
                self.logger.warning("AI returned empty notes")
                return None
                
            return notes
            
        except Exception as e:
            self.logger.error(f"Failed to generate notes: {e}")
            return None
            
    def process_batch(self, segments):
        notes = self.generate_notes(segments)
        
        if notes:
            output_data = {
                'notes': notes,
                'timestamp': datetime.now().isoformat(),
                'segment_count': len(segments),
                'depth_level': self.config['note_taking']['depth_level']
            }
            
            try:
                self.output_queue.put(output_data, timeout=5)
                self.logger.info(f"Generated notes for {len(segments)} segments")
                
                if self.config['output']['log_to_file']:
                    self.log_notes_to_file(notes)
                    
            except Exception as e:
                self.logger.error(f"Failed to queue notes: {e}")
                
        gc.collect()
        
    def log_notes_to_file(self, notes):
        log_file = self.config['files']['notes_log']
        
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"\n\n{'='*80}\n")
                f.write(f"RESEARCH NOTES - {datetime.now().isoformat()}\n")
                f.write(f"{'-'*80}\n")
                f.write(notes)
                f.write(f"\n{'='*80}\n")
                
            self.logger.info(f"Notes logged to {log_file}")
            
        except IOError as e:
            self.logger.error(f"Failed to log notes to file: {e}")
            
    def run(self):
        self.logger.info("Note Generator started")
        self.logger.info(f"Depth level: {self.config['note_taking']['depth_level']}")
        
        last_batch_time = time.time()
        current_batch = []
        
        while not self.shutdown_event.is_set():
            try:
                timeout = max(1, self.batch_timeout - (time.time() - last_batch_time))
                
                try:
                    data = self.input_queue.get(timeout=timeout)
                    
                    if data and 'segments' in data:
                        current_batch.extend(data['segments'])
                        self.logger.info(f"Received {len(data['segments'])} segments")
                        
                        if len(current_batch) >= self.min_batch_size:
                            self.process_batch(current_batch)
                            current_batch = []
                            last_batch_time = time.time()
                            
                except:
                    pass
                    
                if current_batch and (time.time() - last_batch_time) > self.batch_timeout:
                    self.logger.info(f"Processing batch due to timeout ({len(current_batch)} segments)")
                    self.process_batch(current_batch)
                    current_batch = []
                    last_batch_time = time.time()
                    
            except Exception as e:
                self.logger.error(f"Error in generator loop: {e}", exc_info=True)
                time.sleep(5)
                
        if current_batch:
            self.logger.info(f"Processing final batch ({len(current_batch)} segments)")
            self.process_batch(current_batch)
            
        self.logger.info("Note Generator shutting down")
        
if __name__ == "__main__":
    import yaml
    import multiprocessing as mp
    
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)
        
    input_queue = mp.Queue()
    output_queue = mp.Queue()
    shared_state = mp.Manager().dict()
    shutdown_event = mp.Event()
    
    generator = NoteGenerator(config, input_queue, output_queue, shared_state, shutdown_event)
    
    try:
        generator.run()
    except KeyboardInterrupt:
        shutdown_event.set()
        print("Generator stopped")