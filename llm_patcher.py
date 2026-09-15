#!/usr/bin/env python3
import os
import time
import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import threading
import llm_clients

# --- Configuration ---
# Pointing directly to your standalone fuzzer harnesses
HARNESSES = [
    {"name": "PyPNG", "path": "./harness_pypng.py"},
    {"name": "Kaitai", "path": "./harness_kaitai.py"},
    {"name": "Pillow", "path": "./harness_pillow.py"}
]
RESULTS_DIR = "./results"
error_cache = {}
BATCH_TIMEOUT_SECONDS = 60.0 

def trigger_llm_batch(error_signature, fuzzer_context):
    """This fires when we hit 3 files OR when the timer runs out."""
    cache_entry = error_cache.get(error_signature)
    
    # Safety check: Make sure we haven't already processed it
    if not cache_entry or cache_entry["status"] == "waiting_for_llm":
        return
        
    cache_entry["status"] = "waiting_for_llm"
    files_collected = len(cache_entry['files'])
    
    print(f"\n[*] Batch complete! Sending {files_collected} example(s) to Gemini...")
    
    # We append the note to the context that was passed in
    final_context = f"{fuzzer_context}\n\n(Note: Triggered by {files_collected} malformed PNG files.)"
    
    prompt = llm_clients.generate_prompt(
        fuzzer_context=final_context,
        target_file_path="parser/png_ks.py",
        file_format="PNG",
        strict_parser="PyPNG", 
        target_parser="Kaitai Struct" 
    )
    
    gemini_patch = llm_clients.patch_with_gemini(prompt)
    
    print("\n" + "="*50)
    print("[+] GEMINI PATCH SUGGESTION:")
    print("="*50)
    print(gemini_patch)
    print("="*50 + "\n")

# --- Event Handler Class ---
class FuzzerObjectiveHandler(FileSystemEventHandler):
    def check_and_process(self, filepath):
        filename = os.path.basename(filepath)
        
        # Ignore hidden files, temporary files, and metadata files
        if filename.startswith('.') or filename.endswith('.tmp') or filename.endswith('.metadata'):
            return
            
        print(f"\n[+] Valid objective ready: {filepath}")
        time.sleep(0.1) 
        self.process_objective(filepath)

    def on_created(self, event):
        if not event.is_directory:
            self.check_and_process(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.check_and_process(event.dest_path)

    def process_objective(self, filepath):
        try:
            with open(filepath, "rb") as f:
                file_bytes = f.read()
        except Exception as e:
            print(f"[-] Could not read {filepath}: {e}")
            return

        print(f"[*] Analyzing: {os.path.basename(filepath)}")
        
        results = {}
        
        # 1. Execute all harnesses safely via subprocess
        for harness in HARNESSES:
            name = harness["name"]
            try:
                # Pipe the bytes directly into the harness's stdin
                proc = subprocess.run(
                    ["python3", harness["path"]],
                    input=file_bytes,
                    capture_output=True,
                    timeout=2
                )
                
                stdout_text = proc.stdout.decode('utf-8', errors='ignore').strip()
                stderr_text = proc.stderr.decode('utf-8', errors='ignore').strip()

                if "True" in stdout_text:
                    results[name] = {"valid": True, "error": ""}
                else:
                    error_msg = stderr_text if stderr_text else (stdout_text or "Fatal Crash/SegFault")
                    results[name] = {"valid": False, "error": error_msg}

            except subprocess.TimeoutExpired:
                results[name] = {"valid": False, "error": "TimeoutError: Execution exceeded 2 seconds"}

        # 2. Fast-fail for boring structural errors
        for res in results.values():
            if not res["valid"] and "invalid signature" in res["error"].lower():
                return 

        # 3. DISCREPANCY CHECK: Did they actually disagree?
        all_statuses = [res["valid"] for res in results.values()]
        if all(all_statuses):
            return # Everyone agreed it's VALID. No bug.
            
        if not any(all_statuses):
            return # Everyone agreed it's INVALID. No bug.

        # 4. We only reach here if they DISAGREED! Build the signature.
        signature_parts = []
        fuzzer_context = ""
        
        for name, res in results.items():
            if res["valid"]:
                signature_parts.append(f"{name} SUCCEEDED")
            else:
                short_error = str(res["error"]).splitlines()[-1]
                signature_parts.append(f"{name} FAILED: {short_error}")
                fuzzer_context += f"\n[{name} Error]: {res['error']}"

        error_signature = " | ".join(signature_parts)

        # --- Time-Based Batching logic ---
        if error_signature not in error_cache:
            timer = threading.Timer(BATCH_TIMEOUT_SECONDS, trigger_llm_batch, args=[error_signature, fuzzer_context])
            
            error_cache[error_signature] = {
                "status": "collecting",
                "files": [filepath],
                "timer": timer
            }
            timer.start()
            print(f"    -> [New Bug] Started {BATCH_TIMEOUT_SECONDS}s countdown.")
            
        else:
            cache_entry = error_cache[error_signature]
            
            if cache_entry["status"] == "waiting_for_llm":
                print(f"    -> [Ignored] Duplicate bug. Gemini is working on it.")
                return
                
            cache_entry["files"].append(filepath)
            count = len(cache_entry["files"])
            print(f"    -> [Collected] {count}/3 examples.")
            
            if count >= 3:
                cache_entry["timer"].cancel()
                threading.Thread(target=trigger_llm_batch, args=[error_signature, fuzzer_context], daemon=True).start()

if __name__ == "__main__":
    if not os.path.exists(RESULTS_DIR):
        os.makedirs(RESULTS_DIR)

    event_handler = FuzzerObjectiveHandler()
    observer = Observer()
    observer.schedule(event_handler, RESULTS_DIR, recursive=False)
    
    print(f"[*] Starting asynchronous Watcher on {RESULTS_DIR}...")
    print("[*] Waiting for the fuzzer to drop objectives. Press Ctrl+C to stop.")
    
    observer.start()
    
    try:
        while True:
            time.sleep(1) 
    except KeyboardInterrupt:
        print("\n[*] Stopping watcher...")
        observer.stop()
        
    observer.join()
