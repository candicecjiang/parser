#!/usr/bin/env python3
"""
orchestrator.py - fuzz -> diagnose -> patch -> re-fuzz loop, one folder per run.

Each round: run the fuzzer until the first discrepancy, stop it, let the LLM edit
the parser source, then restart (the restart doubles as the re-fuzz).

Every run starts from the pristine parsers in baselines/ and saves to
runs/<timestamp>_<provider>/ : rounds.jsonl, triggers/, patched/, patches/

  python orchestrator.py --init-baselines      # once, while the parsers are pristine
  python orchestrator.py --provider anthropic  # a run
"""
import argparse
import collections
import difflib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import PIL.PngImagePlugin
import png  # pypng

from llm.patch_pipeline import ParserResult, run_pipeline

# ---------------------------------------------------------------- config ----
ROOT = Path(__file__).resolve().parent

FILE_FORMAT = "PNG"

# The fuzzer reads config.toml, ./input and its results dir relative to its
# working directory, so FUZZER_CWD must match wherever it is normally launched.
FUZZER_CWD = ROOT / "fuzzer"
FUZZER_CMD = [str(FUZZER_CWD / "target" / "debug" / "davey-and-daleys-fuzzer")]
FUZZER_RESULTS = ROOT / "results"   # LibAFL scratch space, emptied at the start of every run

RUNS_DIR = ROOT / "runs"
BASELINES = ROOT / "baselines" / "png"

HARNESS_DIR = ROOT / "harnesses" / "png"
# parser name (as used in config.toml) -> (parser source the LLM edits, harness run to validate)
PARSERS = {
    "kaitai": (HARNESS_DIR / "png_ks.py", HARNESS_DIR / "harness_kaitai.py"),
    "pillow": (Path(PIL.PngImagePlugin.__file__), HARNESS_DIR / "harness_pillow.py"),
    "pypng": (Path(png.__file__), HARNESS_DIR / "harness_pypng.py"),
}

# Matches the blocks printed by StdOutDiffObjective:
#   --- pypng_stdout_observer ---
#   RESULT: False | REASON: ChunkError: ...
PARSER_BLOCK = re.compile(
    r"--- (\w+?)_stdout_observer ---\s*\n"
    r"RESULT:\s*(True|False)\s*\|\s*REASON:\s*(.*)"
)


# ------------------------------------------------------------- baselines ----
def baseline_path(name: str) -> Path:
    return BASELINES / name / PARSERS[name][0].name


def init_baselines() -> None:
    """Saves the current parser files as baselines (only ones that don't exist yet)."""
    for name, (src, _) in PARSERS.items():
        dest = baseline_path(name)
        if dest.exists():
            print(f"[=] {name}: baseline already exists, leaving it alone")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        print(f"[+] {name}: saved {src} -> {dest}")


def reset_parsers() -> None:
    """Overwrites the live parser files with their baselines.

    copyfile (not copy2) so the modification time changes and Python
    recompiles instead of reusing a stale .pyc.
    """
    for name, (src, _) in PARSERS.items():
        shutil.copyfile(baseline_path(name), src)


def clear_fuzzer_results() -> None:
    FUZZER_RESULTS.mkdir(parents=True, exist_ok=True)
    for p in FUZZER_RESULTS.iterdir():
        shutil.rmtree(p) if p.is_dir() else p.unlink()


def save_final_parsers(run_dir: Path) -> List[str]:
    """Copies every changed parser into the run folder, with a diff against its baseline."""
    changed = []
    for name, (src, _) in PARSERS.items():
        diff = list(difflib.unified_diff(
            baseline_path(name).read_text().splitlines(keepends=True),
            src.read_text().splitlines(keepends=True),
            fromfile=f"baseline/{src.name}",
            tofile=f"patched/{src.name}",
        ))
        if not diff:
            continue
        dest = run_dir / "patched" / name / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        (run_dir / "patches").mkdir(exist_ok=True)
        (run_dir / "patches" / f"{name}.diff").write_text("".join(diff))
        changed.append(name)
    return changed


# --------------------------------------------------------- fuzzer control ---
def list_result_files() -> Set[Path]:
    """Saved objective inputs (skips LibAFL's hidden metadata files)."""
    if not FUZZER_RESULTS.exists():
        return set()
    return {
        p for p in FUZZER_RESULTS.iterdir()
        if p.is_file() and not p.name.startswith(".")
    }


def snapshot() -> Dict[Path, int]:
    """Maps each saved objective file to its last-modified time."""
    return {p: p.stat().st_mtime_ns for p in list_result_files()}


def stop_fuzzer(proc: subprocess.Popen) -> None:
    """Stop the fuzzer and everything it spawned (forkserver children)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)  # graceful, like Ctrl+C
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def run_until_bug(timeout_s: int) -> Optional[Tuple[List[ParserResult], Path]]:
    """
    Runs the fuzzer until the first discrepancy (or timeout).
    Returns (parser results, path to the triggering input), or None if no bug.
    """
    before = snapshot()

    proc = subprocess.Popen(
        FUZZER_CMD,
        cwd=FUZZER_CWD,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,  # own process group, so we can stop the whole tree
    )
    timer = threading.Timer(timeout_s, stop_fuzzer, args=(proc,))
    timer.start()

    tail = collections.deque(maxlen=20)  # last output lines, for debugging
    block: List[str] = []
    in_diff = False
    found = False

    try:
        for line in proc.stdout:
            tail.append(line)
            if line.startswith("[DIFF] Full outputs"):
                in_diff, block = True, []
            elif in_diff and line.startswith("[Objective"):
                found = True
                break
            elif in_diff:
                block.append(line)
    finally:
        timer.cancel()
        stop_fuzzer(proc)

    if not found:
        print(f"[*] Last fuzzer status: {tail[-1].strip() if tail else '(no output)'}")
        if proc.returncode not in (0, -signal.SIGINT, -signal.SIGKILL):
            print("[!] Fuzzer exited unexpectedly. Last output:")
            print("".join(tail))
        return None

    # Parse each parser's verdict + reason out of the [DIFF] block
    results: List[ParserResult] = []
    for m in PARSER_BLOCK.finditer("".join(block)):
        name, verdict, reason = m.groups()
        if name not in PARSERS:
            print(f"[!] Fuzzer reported unknown parser {name!r}; add it to PARSERS")
            continue
        source_path, harness_path = PARSERS[name]
        results.append(
            ParserResult(
                parser_name=name,
                verdict=(verdict == "True"),
                reason=reason.strip(),
                source_path=str(source_path),
                harness_path=str(harness_path),
            )
        )

    # The triggering input: the file added or modified since we started
    after = snapshot()
    if not after:
        print("[!] Objective found but no saved input in", FUZZER_RESULTS)
        return None
    changed = [p for p, m in after.items() if before.get(p) != m]
    crash_file = max(changed or after, key=lambda p: after[p])

    return results, crash_file


# ------------------------------------------------------------------ main ----
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="gemini",
                    choices=["gemini", "openai", "anthropic"])
    ap.add_argument("--max-rounds", type=int, default=10)
    ap.add_argument("--fuzz-timeout", type=int, default=600,
                    help="seconds to fuzz per round before giving up")
    ap.add_argument("--init-baselines", action="store_true",
                    help="save the current parser files as baselines, then exit")
    args = ap.parse_args()

    if args.init_baselines:
        init_baselines()
        return

    missing = [n for n in PARSERS if not baseline_path(n).exists()]
    if missing:
        sys.exit(f"Missing baselines for {missing}. Run: python orchestrator.py --init-baselines")

    run_dir = RUNS_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{args.provider}"
    triggers_dir = run_dir / "triggers"
    triggers_dir.mkdir(parents=True)
    log_path = run_dir / "rounds.jsonl"

    reset_parsers()          # every run starts from the pristine parsers
    clear_fuzzer_results()   # and an empty LibAFL results directory
    print(f"[*] Run folder: {run_dir}")

    try:
        for round_num in range(1, args.max_rounds + 1):
            print(f"\n===== Round {round_num}/{args.max_rounds}: fuzzing =====")

            found = run_until_bug(args.fuzz_timeout)
            if found is None:
                print(f"[*] No discrepancy within {args.fuzz_timeout}s. Stopping.")
                break

            results, crash_file = found
            if len(results) < 2:
                print("[!] Could not parse parser outputs from the fuzzer log. Stopping.")
                break

            trigger = triggers_dir / f"round_{round_num:03d}_{crash_file.name}"
            shutil.copy2(crash_file, trigger)

            print(f"[*] Bug found. Trigger saved to {trigger}")
            for r in results:
                print(f"    {r.parser_name}: {r.verdict} | {r.reason[:100]}")

            try:
                outcome = run_pipeline(
                    results, FILE_FORMAT, str(trigger), provider=args.provider
                )
            except Exception as e:  # bad LLM JSON, API error, etc.
                print(f"[!] Patch pipeline failed: {type(e).__name__}: {e}")
                break

            record = {
                "round": round_num,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "trigger": str(trigger.relative_to(run_dir)),
                "parsers": {
                    r.parser_name: {"verdict": r.verdict, "reason": r.reason}
                    for r in results
                },
                **asdict(outcome),
            }
            with open(log_path, "a") as f:
                f.write(json.dumps(record) + "\n")

            if not outcome.successful_patches:
                print("[-] No patch passed validation. Stopping for manual inspection.")
                break

            print("[+] Patched. Restarting fuzzer (this is the re-fuzz)...")
        else:
            print(f"[*] Reached max rounds ({args.max_rounds}). Stopping.")
    finally:
        # Runs on normal exit, errors and Ctrl+C, so the patched parsers are always saved
        changed = save_final_parsers(run_dir)
        print(f"\n[*] Run saved to {run_dir}. Parsers changed: {', '.join(changed) or 'none'}")


if __name__ == "__main__":
    main()