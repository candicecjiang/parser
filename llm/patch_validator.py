import subprocess
import os
import re
import sys
import signal
import time
from pathlib import Path


def validate_patch(target_path: str, crash_file_path: str, expected_verdict: bool) -> bool:
    """
    STAGE 1 — Cheap validation.
    Validates an LLM-generated patch by attempting to compile it (if applicable)
    and running the target parser against the single crash-triggering file,
    checking that its verdict now matches the expected (reference) verdict.
    """

    # --- Compilation Check (Kaitai Specific) ---
    if target_path.endswith(".ksy"):
        print(f"[*] Validating Kaitai schema patch: {target_path}")
        compile_cmd = ["ksc", "-t", "python", target_path]

        try:
            subprocess.run(compile_cmd, capture_output=True, text=True, check=True)
            print("[+] Kaitai compilation successful.")
            harness_path = os.path.join(os.path.dirname(target_path), "harness_kaitai.py")

        except subprocess.CalledProcessError as e:
            print(f"[-] Compilation failed. The LLM wrote invalid YAML or Kaitai syntax:\n{e.stderr}")
            return False
    else:
        harness_path = target_path

    # --- Execution Check (single triggering input) ---
    print(f"[*] Running harness {harness_path} against {crash_file_path}")

    try:
        with open(crash_file_path, "rb") as f:
            crash_bytes = f.read()
    except FileNotFoundError:
        print(f"[-] Validation failed. Could not find crash file at {crash_file_path}.")
        return False

    run_cmd = [sys.executable, harness_path]

    try:
        result = subprocess.run(
            run_cmd,
            input=crash_bytes,
            capture_output=True,
            timeout=5
        )

        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")

        if result.returncode != 0:
            print(f"[-] Validation execution failed (Return code {result.returncode}):\n{stderr_text}")
            return False

        match = re.search(r"RESULT:\s*(True|False)", stdout_text)
        if not match:
            print(f"[-] Validation failed. Harness did not produce expected output format:\n{stdout_text}")
            return False

        actual_verdict = match.group(1) == "True"

        if actual_verdict != expected_verdict:
            print(
                f"[-] Patch did not resolve the discrepancy. "
                f"Expected verdict={expected_verdict}, got={actual_verdict}\n{stdout_text.strip()}"
            )
            return False

        print(f"[+] Patch verified against triggering input (expected={expected_verdict}).")
        return True

    except subprocess.TimeoutExpired:
        print("[-] Validation failed. Patch caused a timeout (possible infinite loop).")
        return False
    except FileNotFoundError:
        print(f"[-] Validation failed. Could not find harness script at {harness_path}.")
        return False


class FuzzerProcess:
    """Manages the lifecycle of the Rust fuzzer binary as a subprocess."""

    def __init__(self, fuzzer_binary_path: str, cwd: str = None):
        self.fuzzer_binary_path = fuzzer_binary_path
        self.cwd = cwd
        self.process: subprocess.Popen | None = None

    def start(self):
        print(f"[*] Starting fuzzer: {self.fuzzer_binary_path}")
        self.process = subprocess.Popen(
            [self.fuzzer_binary_path],
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def stop(self, timeout: float = 5.0):
        if self.process is None:
            return
        print("[*] Stopping fuzzer...")
        self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print("[!] Fuzzer did not exit gracefully, killing it.")
            self.process.kill()
            self.process.wait()
        self.process = None

    def restart(self):
        self.stop()
        self.start()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None


def count_current_objectives(diffs_dir: str = "results/diffs") -> int:
    """Counts how many diff records currently exist, used as a before/after snapshot."""
    path = Path(diffs_dir)
    if not path.exists():
        return 0
    return len(list(path.glob("*.json")))


def refuzz_and_check(
    fuzzer_binary_path: str,
    fuzzer_cwd: str,
    duration_seconds: int = 60,
    diffs_dir: str = "results/diffs",
) -> bool:
    """
    STAGE 2 — Full re-fuzz.
    Restarts the fuzzer (with the patched parser already in place) and lets it
    run for a bounded duration. Returns True if no NEW discrepancies appeared
    during the re-fuzz window (i.e. the patch held up), False otherwise.

    Assumes the fuzzer resumes from / continues writing into the same
    `results/diffs` directory used for objective diff records.
    """
    baseline_count = count_current_objectives(diffs_dir)
    print(f"[*] Starting re-fuzz for {duration_seconds}s (baseline objectives: {baseline_count})")

    fuzzer = FuzzerProcess(fuzzer_binary_path, cwd=fuzzer_cwd)
    fuzzer.start()

    try:
        time.sleep(duration_seconds)
    finally:
        fuzzer.stop()

    new_count = count_current_objectives(diffs_dir)
    new_objectives = new_count - baseline_count

    if new_objectives > 0:
        print(f"[-] Re-fuzz found {new_objectives} new discrepancy(ies). Patch may have regressed or the bug resurfaced.")
        return False

    print("[+] Re-fuzz completed with no new discrepancies. Patch holds up.")
    return True


def validate_patch_full(
    target_path: str,
    crash_file_path: str,
    expected_verdict: bool,
    fuzzer_binary_path: str,
    fuzzer_cwd: str,
    refuzz_duration_seconds: int = 60,
) -> bool:
    """
    Runs the full two-stage validation:
    1. Cheap single-input check.
    2. Full re-fuzz, only if stage 1 passes.
    """
    if not validate_patch(target_path, crash_file_path, expected_verdict):
        print("[-] Skipping re-fuzz stage; cheap validation already failed.")
        return False

    return refuzz_and_check(fuzzer_binary_path, fuzzer_cwd, refuzz_duration_seconds)