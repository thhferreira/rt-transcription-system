import json
import yaml
import time
import subprocess
import os
from datetime import datetime, timedelta
from openai import OpenAI, APIConnectionError, RateLimitError, APIStatusError, APITimeoutError
import httpx
import threading
import traceback

# Import and initialize Colorama
try:
    import colorama
    from colorama import Fore, Style, Back
    colorama.init(autoreset=True)
    USE_COLORAMA = True
except ImportError:
    print("Colorama library not found. Output will not be colored. Install with: pip install colorama")
    # Define dummy Fore, Style, Back if colorama is not available
    class DummyColor:
        def __getattr__(self, name):
            return ""
    Fore = Style = Back = DummyColor()
    USE_COLORAMA = False

class TranscriptMonitor:
    def __init__(self, config_path="config.yaml"):
        print(Fore.CYAN + f"Attempting to load configuration from: {config_path}")
        if not os.path.exists(config_path):
            print(Fore.RED + Style.BRIGHT + f"FATAL ERROR: Configuration file '{config_path}' not found.")
            raise FileNotFoundError(f"Configuration file '{config_path}' not found.")
            
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)
        
        deepseek_cfg = self.config.get('deepseek', {})
        api_key_value = deepseek_cfg.get('api_key')
        if not api_key_value or api_key_value == "YOUR_DEEPSEEK_API_KEY_HERE": # Check your placeholder
            print(Fore.RED + Style.BRIGHT + "FATAL ERROR: DeepSeek API key not set or is placeholder in config.yaml.")
            print(Fore.YELLOW + "Please set your deepseek.api_key in the configuration file.")
            raise ValueError("DeepSeek API key not configured.")

        self.client = OpenAI(
            api_key=api_key_value,
            base_url=deepseek_cfg.get('base_url', "https://api.deepseek.com"),
            max_retries=deepseek_cfg.get('max_retries', 3), 
            timeout=httpx.Timeout(
                deepseek_cfg.get('timeout_connect', 15.0),
                read=deepseek_cfg.get('timeout_read', 60.0),
                write=deepseek_cfg.get('timeout_write', 10.0),
                pool=deepseek_cfg.get('timeout_pool', 5.0)
            )
        )
        self.last_processed_time = self.load_last_processed_time()
        
        print(Fore.GREEN + "Transcript Monitor initialized successfully (using DeepSeek API via OpenAI SDK).")
        print(Fore.BLUE + f" - DeepSeek Model: {deepseek_cfg.get('model', 'deepseek-chat')}")
        print(Fore.BLUE + f" - DeepSeek Client: Connect Timeout={deepseek_cfg.get('timeout_connect', 15.0)}s, Read Timeout={deepseek_cfg.get('timeout_read', 60.0)}s")
        print(Fore.BLUE + f" - Monitoring base transcript dir: {self.config['files']['transcript_path']}")
        print(Fore.BLUE + f" - Note-taking depth: {self.config['note_taking']['depth_level']}")
        last_processed_str = datetime.fromtimestamp(self.last_processed_time).isoformat() if self.last_processed_time > 0 else 'Never processed'
        print(Fore.BLUE + f" - Last processed timestamp loaded: {self.last_processed_time:.2f} (Epoch) -> {last_processed_str}")


    def load_last_processed_time(self):
        log_file = self.config['files']['processed_log']
        if os.path.exists(log_file):
            try:
                with open(log_file, 'r') as f:
                    content = f.read().strip()
                    if content: return float(content)
                    print(Fore.YELLOW + f"Warning: Processed log file '{log_file}' is empty. Starting from beginning.")
                    return 0.0
            except ValueError:
                print(Fore.YELLOW + f"Warning: Could not parse timestamp from {log_file}. Starting from beginning.")
                return 0.0
            except FileNotFoundError: # Should be caught by os.path.exists
                return 0.0
        print(Fore.YELLOW + f"Processed log file '{log_file}' not found. Starting from beginning.")
        return 0.0

    def save_last_processed_time(self, timestamp_float):
        log_file = self.config['files']['processed_log']
        try:
            with open(log_file, 'w') as f:
                f.write(str(timestamp_float))
        except Exception as e:
            print(Fore.RED + f"Error saving last processed time to '{log_file}': {e}")


    def get_latest_transcript_file_path(self):
        base_transcript_dir = self.config['files']['transcript_path']
        if not os.path.isdir(base_transcript_dir): 
            # This can be normal if rt_transcribe hasn't run yet or created the dir
            # print(Fore.YELLOW + f"Warning: Base transcript directory not found or not a directory: {base_transcript_dir}")
            return None
        try:
            session_dirs = [d for d in os.listdir(base_transcript_dir) if os.path.isdir(os.path.join(base_transcript_dir, d)) and d.startswith("session_")]
        except FileNotFoundError: return None # Base dir disappeared
        if not session_dirs: return None # No session subdirectories yet
        
        latest_session_dir_name = max(session_dirs) # Assumes YYYYMMDD-HHMMSS sorts correctly
        latest_session_path = os.path.join(base_transcript_dir, latest_session_dir_name)
        
        try:
            transcript_json_files = [f for f in os.listdir(latest_session_path) if f.startswith("transcript_chunks_") and f.endswith(".json")]
        except FileNotFoundError:
            print(Fore.YELLOW + f"Warning: Latest session directory '{latest_session_path}' disappeared unexpectedly.")
            return None
        if not transcript_json_files: 
            # print(Fore.YELLOW + f"No transcript_chunks JSON file found in latest session: {latest_session_path}") # Can be noisy
            return None
        return os.path.join(latest_session_path, transcript_json_files[0]) # Assuming one such file

    def get_recent_transcript_segments(self):
        path_to_transcript_json = self.get_latest_transcript_file_path()
        if not path_to_transcript_json: return []
        
        try:
            with open(path_to_transcript_json, 'r', encoding='utf-8') as f:
                all_segments_from_file = json.load(f)
            
            recent_segments_for_notes = []
            if not isinstance(all_segments_from_file, list):
                print(Fore.YELLOW + f"Warning: Transcript file {path_to_transcript_json} is not a list as expected by the script.")
                return []

            for live_chunk_data in all_segments_from_file:
                iso_timestamp_str = live_chunk_data.get('timestamp')
                raw_text = live_chunk_data.get('raw_transcript')
                if not iso_timestamp_str or raw_text is None: continue # Skip if essential data missing
                
                try:
                    dt_object = datetime.fromisoformat(iso_timestamp_str)
                    segment_start_epoch_float = dt_object.timestamp()
                except ValueError:
                    print(Fore.YELLOW + f"Warning: Could not parse ISO timestamp '{iso_timestamp_str}' for chunk ID {live_chunk_data.get('chunk_id', 'N/A')}. Skipping.")
                    continue
                
                if segment_start_epoch_float > self.last_processed_time:
                    recent_segments_for_notes.append({'start': segment_start_epoch_float, 'text': raw_text})
            
            recent_segments_for_notes.sort(key=lambda x: x['start']) # Ensure chronological order
            return recent_segments_for_notes
            
        except FileNotFoundError: 
            # print(Fore.YELLOW + f"Transcript file not found during read attempt: {path_to_transcript_json}") # Can be noisy
            return []
        except json.JSONDecodeError as e:
            print(Fore.RED + f"Error decoding JSON from transcript file '{path_to_transcript_json}': {e}")
            return []
        except Exception as e_general:
            print(Fore.RED + f"An unexpected error occurred while getting recent transcript segments from '{path_to_transcript_json}':")
            traceback.print_exc()
            return []

    def format_transcript_for_ai(self, segments_for_notes):
        if not segments_for_notes: return None
        transcript_text_for_prompt = ""
        for segment in segments_for_notes:
            segment_epoch_time = segment['start']
            segment_text = segment['text']
            precision = self.config['format']['timestamp_precision']
            time_format = '%H:%M:%S' if precision == 'second' else '%H:%M'
            formatted_timestamp = datetime.fromtimestamp(segment_epoch_time).strftime(time_format)
            transcript_text_for_prompt += f"[{formatted_timestamp}] {segment_text}\n"
        return transcript_text_for_prompt.strip()

    def _prepare_messages_for_deepseek(self, system_instructions: str, user_input_text: str):
        return [
            {"role": "system", "content": system_instructions.strip()},
            {"role": "user", "content": user_input_text.strip()}
        ]

    # --- Note Generation Prompts (Unchanged from previous DeepSeek version) ---
    def get_minimal_notes(self, transcript_text):
        system_instructions = """
        You are an assistant that creates minimal factual notes from a transcript.
        Capture WHAT was said, not WHY. Focus on:
        - Direct statements made
        - Actions mentioned
        - Specific details shared
        - Brief quotes
        Format the notes as follows, using the timestamps from the provided transcript text:
        [Time] STATED: What participant said
        [Time] ACTION: What participant did
        [Time] DETAIL: Specific fact mentioned
        Provide NO analysis or interpretation. Just facts for researcher reference.
        """
        messages = self._prepare_messages_for_deepseek(system_instructions, transcript_text)
        return self.call_deepseek(messages=messages)
    
    def get_standard_notes(self, transcript_text):
        system_instructions = """
        You are an assistant that creates factual notes with minimal context from a transcript.
        Focus on WHAT happened:
        - What participant said (verbatim when significant)
        - What participant did (behaviors, actions)
        - Specific details, numbers, names mentioned
        - Light context only when essential for clarity
        Format the notes as follows, using the timestamps from the provided transcript text:
        [Time] SAID: Direct statement or paraphrase
        [Time] DID: Action or behavior described
        [Time] MENTIONED: Specific detail, number, brand, etc.
        [Time] CONTEXT: Minimal situational detail (only if needed)
        Keep interpretation to an absolute minimum. Facts first.
        """
        messages = self._prepare_messages_for_deepseek(system_instructions, transcript_text)
        return self.call_deepseek(messages=messages)

    def get_detailed_notes(self, transcript_text):
        system_instructions = """
        You are an assistant that creates detailed factual notes from a transcript.
        Focus on comprehensive WHAT without heavy WHY:
        - Complete statements with context
        - All actions and behaviors mentioned
        - Specific details, numbers, brands, timelines
        - Full quotes when significant
        - Sequence of events as described
        - Light analysis only for immediate clarity
        Format the notes as follows, using the timestamps from the provided transcript text:
        [Time] STATEMENT: Complete description of what was said
        [Time] BEHAVIOR: Full description of actions taken
        [Time] DETAILS: Specific facts, figures, brands mentioned
        [Time] SEQUENCE: Order of events as described
        [Time] QUOTE: "Full verbatim statement" - brief context note
        Provide comprehensive facts with minimal interpretation.
        """
        messages = self._prepare_messages_for_deepseek(system_instructions, transcript_text)
        return self.call_deepseek(messages=messages)

    def get_comprehensive_notes(self, transcript_text):
        system_instructions = """
        You are an assistant that creates comprehensive factual documentation from a transcript.
        Focus on:
        - Every significant statement made
        - All behaviors, actions, and reactions described
        - Complete timeline and sequence of events
        - All specific details: numbers, brands, people, places
        - Full verbatim quotes
        - Situational context provided by participant
        - Process descriptions as given
        - Minimal analysis - only for factual clarity
        Format the notes as follows, using the timestamps from the provided transcript text:
        [Time] VERBATIM: "Complete quote as stated"
        [Time] DESCRIBED: Full description of situation/process as explained
        [Time] REPORTED: Actions, behaviors, or events participant reported
        [Time] SPECIFIED: Exact details, numbers, brands, timelines mentioned
        [Time] CONTEXT: Situational background provided by participant
        Provide a complete factual record for thorough analysis later.
        """
        messages = self._prepare_messages_for_deepseek(system_instructions, transcript_text)
        return self.call_deepseek(messages=messages)

    def call_deepseek(self, messages: list):
        deepseek_cfg = self.config.get('deepseek', {})
        model_name = deepseek_cfg.get('model', 'deepseek-chat')
        # max_tokens for chat completions, if you add it to config.yaml
        max_tokens_completion = deepseek_cfg.get('max_tokens_completion', 1500) # Default if not in config

        try:
            current_time_str = datetime.now().strftime('%H:%M:%S')
            print(Fore.MAGENTA + f"[{current_time_str}] Calling DeepSeek API (model: '{model_name}')...")
            
            user_message_content = next((msg.get("content","") for msg in messages if msg.get("role") == "user"), "")
            if not user_message_content.strip():
                print(Fore.YELLOW + f"[{current_time_str}] No user input text provided to DeepSeek. Skipping API call.")
                return "Error: No transcript text provided for note generation."

            response = self.client.chat.completions.create(
                model=model_name,
                messages=messages,
                stream=False,
                max_tokens=max_tokens_completion 
            )
            
            generated_notes = response.choices[0].message.content
            print(Fore.GREEN + f"[{current_time_str}] DeepSeek API call successful.")
            if not generated_notes:
                 print(Fore.YELLOW + f"[{current_time_str}] Warning: DeepSeek returned empty content.")
                 return "Warning: DeepSeek returned empty notes."
            return generated_notes
        except APITimeoutError as e:
            print(Fore.RED + f"DeepSeek API Request Timed Out: {e}")
            traceback.print_exc()
            return f"Error (Timeout): DeepSeek API request timed out. Details: {e}"
        except APIConnectionError as e:
            print(Fore.RED + f"DeepSeek API Connection Error: {e}")
            traceback.print_exc()
            return f"Error (Connection): Could not connect to DeepSeek API. Details: {e}"
        except RateLimitError as e:
            print(Fore.RED + f"DeepSeek API Rate Limit Error: {e}")
            traceback.print_exc()
            return f"Error (Rate Limit): DeepSeek API request failed. Details: {e}"
        except APIStatusError as e: # This catches HTTP status errors like 4xx, 5xx
            error_message = getattr(e, 'message', str(e))
            print(Fore.RED + f"DeepSeek API Status Error (Status Code: {e.status_code}): {error_message}")
            traceback.print_exc()
            return f"Error (API Status {e.status_code}): DeepSeek API request failed. Details: {error_message}"
        except Exception as e: # General catch-all
            print(Fore.RED + f"An unexpected error occurred during DeepSeek API call:")
            traceback.print_exc()
            return f"Error (Unexpected): Failed to generate notes via DeepSeek API. Details: {str(e)}"

    def generate_notes_from_segments(self, segments_to_process):
        depth_level = self.config['note_taking']['depth_level']
        transcript_text_for_ai = self.format_transcript_for_ai(segments_to_process)
        if not transcript_text_for_ai:
            print(Fore.YELLOW + "No text formatted for AI. Skipping note generation.")
            return None
        current_time_str = datetime.now().strftime('%H:%M:%S')
        print(Fore.CYAN + f"[{current_time_str}] Generating notes with depth: {depth_level}...")
        if depth_level == "minimal": return self.get_minimal_notes(transcript_text_for_ai)
        elif depth_level == "standard": return self.get_standard_notes(transcript_text_for_ai)
        elif depth_level == "detailed": return self.get_detailed_notes(transcript_text_for_ai)
        elif depth_level == "comprehensive": return self.get_comprehensive_notes(transcript_text_for_ai)
        else:
            print(Fore.YELLOW + f"Warning: Unknown depth_level '{depth_level}'. Defaulting to 'standard'.")
            return self.get_standard_notes(transcript_text_for_ai)

    def display_in_new_terminal(self, content_to_display):
        current_time_str = datetime.now().strftime('%H:%M:%S')
        try:
            # Escape for AppleScript string literals AND shell command embedded in 'do script'
            # 1. Escape backslashes for AppleScript
            escaped = content_to_display.replace('\\', '\\\\')
            # 2. Escape double quotes for AppleScript string AND for shell echo
            escaped = escaped.replace('"', '\\"')
            # 3. Escape dollar signs for shell
            escaped = escaped.replace('$', '\\$')
            # 4. Escape backticks for shell
            escaped = escaped.replace('`', '\\`')
            # 5. Convert Python newlines to AppleScript newlines for the 'echo' command
            escaped = escaped.replace('\n', '\\n')


            script = f'''
            tell application "Terminal"
                if not (exists window 1) then reopen
                activate
                do script "clear && echo \\"{escaped}\\" && echo \\"\\\\n--- Research Notes Generated at {current_time_str} (DeepSeek) ---\\" && echo \\"\\\\n(Terminal may close or press Ctrl+C to close sooner)\\" && sleep 5"
            end tell'''
            
            # For debugging AppleScript:
            # print(Fore.MAGENTA + "---- Generated AppleScript ----")
            # print(script)
            # print("-----------------------------")

            subprocess.run(['osascript', '-e', script], check=True, timeout=15) # Increased timeout slightly
            print(Fore.GREEN + f"[{current_time_str}] Notes displayed in new terminal.")
        except subprocess.TimeoutExpired:
            print(Fore.YELLOW + f"[{current_time_str}] New terminal display command timed out. Notes were likely displayed but script didn't wait.")
        except subprocess.CalledProcessError as e:
            print(Fore.RED + f"Error running AppleScript for new terminal (Code: {e.returncode}): {e}")
            # print(Fore.RED + f"Failed AppleScript was:\n{script}") # Uncomment to see the script that failed
            self.fallback_display(content_to_display)
        except Exception as e:
            print(Fore.RED + f"General error displaying in new terminal: {e}")
            self.fallback_display(content_to_display)

    def fallback_display(self, content_to_display):
        print(Fore.YELLOW + "\n" + "="*60 + Style.BRIGHT + "\nRESEARCH NOTES (Fallback Display)" + Style.NORMAL + "\n" + "="*60)
        print(content_to_display)
        print("="*60)

    def log_notes_to_file(self, notes_content):
        if self.config['output']['log_to_file']:
            log_file_path = self.config['files']['notes_log']
            try:
                with open(log_file_path, 'a', encoding='utf-8') as f:
                    f.write(f"\n\n{'='*80}\nRESEARCH NOTES - {datetime.now().isoformat()} (DeepSeek)\n{'-'*80}\n{notes_content}\n{'='*80}\n")
                print(Fore.GREEN + f"[{datetime.now().strftime('%H:%M:%S')}] Notes appended to: {log_file_path}")
            except Exception as e:
                print(Fore.RED + f"Error logging notes to file '{log_file_path}': {e}")

    def process_transcript_and_generate_notes(self):
        current_time_str = datetime.now().strftime('%H:%M:%S')
        print(Style.BRIGHT + Fore.CYAN + f"\n[{current_time_str}] Checking for new transcript segments...")
        new_segments = self.get_recent_transcript_segments()
        if not new_segments:
            print(Fore.GREEN + f"[{current_time_str}] No new transcript segments to process.")
            return
        
        print(Fore.GREEN + f"[{current_time_str}] Found {len(new_segments)} new segment(s).")
        generated_notes = self.generate_notes_from_segments(new_segments)
        
        if not generated_notes or "Error" in generated_notes[:30] or "Warning:" in generated_notes[:30]: # Broader check for issues
            print(Fore.RED + f"[{current_time_str}] Failed to generate notes or error/warning occurred. Details: {generated_notes}")
            # Optionally, decide if you still want to update last_processed_time or retry
            return 
            
        if self.config['output']['new_terminal']:
            self.display_in_new_terminal(generated_notes)
        else:
            self.fallback_display(generated_notes)
        
        self.log_notes_to_file(generated_notes)
        
        if new_segments: # Should be true if notes were generated
            latest_segment_time_epoch = new_segments[-1]['start'] # Segments are sorted
            self.save_last_processed_time(latest_segment_time_epoch)
            print(Fore.GREEN + f"[{current_time_str}] Last processed time updated to: {datetime.fromtimestamp(latest_segment_time_epoch).isoformat()}")
        
        print(Style.BRIGHT + Fore.GREEN + f"[{current_time_str}] ✓ Research notes generation cycle complete!")

    def start_monitoring_loop(self):
        interval_seconds = self.config['monitoring']['interval_minutes'] * 60
        header = Style.BRIGHT + Fore.BLUE + f"\n{'='*60}\nREAL-TIME QUALITATIVE NOTES MONITOR (DeepSeek API)\n{'='*60}" + Style.RESET_ALL
        print(header)
        print(f"Starting monitoring. Interval: {self.config['monitoring']['interval_minutes']} min(s).")
        print(f" - Monitoring base transcript dir: {self.config['files']['transcript_path']}")
        print(f" - Notes log: {self.config['files']['notes_log']}")
        print(f" - Processed timestamps log: {self.config['files']['processed_log']}")
        print(Fore.YELLOW + "Press Ctrl+C to stop monitoring.\n" + "="*60)
        try:
            while True:
                self.process_transcript_and_generate_notes()
                next_check_dt = datetime.now() + timedelta(seconds=interval_seconds)
                print(Fore.CYAN + f"[{datetime.now().strftime('%H:%M:%S')}] Next check at {next_check_dt.strftime('%H:%M:%S')}. Sleeping...")
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print(Fore.YELLOW + "\n\nMonitoring stopped by user.")
        except Exception as e_loop: # Catch any other unexpected error in the main loop
            print(Fore.RED + Style.BRIGHT + f"\nFATAL ERROR in monitoring loop:")
            traceback.print_exc()

def main():
    try:
        monitor = TranscriptMonitor(config_path="config.yaml") # Or pass a different path
        monitor.start_monitoring_loop()
    except FileNotFoundError as e: # Specifically for config file not found
        print(Fore.RED + Style.BRIGHT + f"CRITICAL ERROR: {e}")
        print(Fore.YELLOW + "Please ensure 'config.yaml' exists in the current directory or provide the correct path.")
    except ValueError as e: # Specifically for API key or other critical config misconfiguration
        print(Fore.RED + Style.BRIGHT + f"CRITICAL CONFIGURATION ERROR: {e}")
    except Exception as e_main:
        print(Fore.RED + Style.BRIGHT + f"An unexpected error occurred during monitor setup or main execution:")
        traceback.print_exc()

if __name__ == "__main__":
    main()