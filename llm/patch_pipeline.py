"""
llm/patch_pipeline.py

Everything between "the fuzzer found a bug" and "the parser is patched":
build context -> ask the LLM -> apply edits in memory -> validate -> roll back on failure.

Nothing is written to disk until every edit has matched exactly once and every
edited file still compiles. If validation fails afterwards, the original file
contents (held in memory) are restored, so no .bak files are needed.
"""
import binascii
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .llm_clients import generate_prompt, generate_patch

MAX_OLD_LINES = 30  # an "old" block longer than this is a whole-function rewrite, not an edit


# ------------------------------------------------------------------ types ---
@dataclass
class ParserResult:
    parser_name: str
    verdict: bool
    reason: str
    source_path: str    # parser source the LLM may edit
    harness_path: str   # read-only wrapper, only executed for validation


@dataclass
class PipelineResult:
    successful_patches: List[str] = field(default_factory=list)
    failed_patches: List[str] = field(default_factory=list)
    diagnosis: str = ""
    provider: str = ""
    correct_verdict: Optional[bool] = None
    edits: List[dict] = field(default_factory=list)  # the patch record, success or not


class EditError(Exception):
    pass


# ---------------------------------------------------------------- context ---
def read_sources(results: List[ParserResult], attr: str) -> Dict[str, str]:
    """Reads each parser's `source_path` or `harness_path` into {parser_name: text}."""
    sources = {}
    for r in results:
        path = getattr(r, attr)
        try:
            sources[r.parser_name] = Path(path).read_text()
        except FileNotFoundError:
            sources[r.parser_name] = f"Error: could not read {path}"
    return sources


def get_hex_window(file_path: str, offset: int = 0, window_size: int = 64) -> str:
    """Hex dump of the triggering file around an offset."""
    try:
        with open(file_path, "rb") as f:
            f.seek(max(0, offset - window_size))
            chunk = f.read(window_size * 2)
        return binascii.hexlify(chunk, sep=b" ", bytes_per_sep=1).decode("ascii")
    except FileNotFoundError:
        return f"Error: could not read crash file at {file_path}"


# ------------------------------------------------------------- validation ---
def check_harness(harness_path: str, crash_file: str, expected_verdict: bool) -> bool:
    """Runs one harness on the triggering input and checks its verdict."""
    try:
        crash_bytes = Path(crash_file).read_bytes()
    except FileNotFoundError:
        print(f"[-] Could not find crash file at {crash_file}")
        return False

    try:
        result = subprocess.run(
            [sys.executable, harness_path],
            input=crash_bytes,       # harnesses read stdin, not argv
            capture_output=True,
            timeout=5,               # catches patch-induced infinite loops
        )
    except subprocess.TimeoutExpired:
        print(f"[-] {Path(harness_path).name}: timed out (possible infinite loop).")
        return False

    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")

    if result.returncode != 0:  # syntax errors / bad imports in a patched file land here
        print(f"[-] {Path(harness_path).name}: crashed (code {result.returncode}):\n{stderr}")
        return False

    match = re.search(r"RESULT:\s*(True|False)", stdout)
    if not match:
        print(f"[-] {Path(harness_path).name}: no RESULT line in output:\n{stdout}")
        return False

    actual = match.group(1) == "True"
    if actual != expected_verdict:
        print(f"[-] {Path(harness_path).name}: expected {expected_verdict}, got {actual}")
        return False

    return True


# ------------------------------------------------------------------ edits ---
def prepare_edits(
    edits: List[dict], parser_paths: Dict[str, Path]
) -> Tuple[Dict[Path, str], Dict[Path, str]]:
    """
    Applies edits in memory. Returns (originals, patched) keyed by file path.
    Raises EditError if an edit is malformed, targets a non-patchable parser,
    is oversized, doesn't match exactly once, or leaves a file that won't compile.
    """
    originals: Dict[Path, str] = {}
    patched: Dict[Path, str] = {}

    for i, edit in enumerate(edits):
        try:
            name, old, new = edit["parser"], edit["old"], edit["new"]
        except (KeyError, TypeError):
            raise EditError(f"edit {i} is missing 'parser', 'old', or 'new'")

        if name not in parser_paths:
            raise EditError(f"edit {i} targets {name!r}, which is not a patchable parser")
        if not isinstance(old, str) or not isinstance(new, str) or not old.strip():
            raise EditError(f"edit {i} has an empty or non-string 'old'/'new' block")
        if old.count("\n") > MAX_OLD_LINES:
            raise EditError(f"edit {i} ({name}): 'old' block is too large ({old.count(chr(10))} lines)")

        path = parser_paths[name]
        if path not in patched:
            originals[path] = path.read_text()
            patched[path] = originals[path]

        count = patched[path].count(old)
        if count != 1:
            raise EditError(f"edit {i} ({name}): 'old' text matched {count} times, need exactly 1")
        patched[path] = patched[path].replace(old, new, 1)

    for path, text in patched.items():
        try:
            compile(text, str(path), "exec")
        except SyntaxError as e:
            raise EditError(f"{path.name} would not compile after the edits: {e}")

    return originals, patched


def apply_and_validate(
    edits: List[dict],
    results: List[ParserResult],
    crash_file: str,
    expected_verdict: bool,
) -> bool:
    """
    Applies the edits, then requires EVERY parser's harness to return
    expected_verdict on the triggering input (i.e. the discrepancy is resolved).
    Restores the original files if anything fails.
    """
    parser_paths = {r.parser_name: Path(r.source_path) for r in results}

    try:
        originals, patched = prepare_edits(edits, parser_paths)
    except EditError as e:
        print(f"[-] Rejected edits before touching any file: {e}")
        return False

    for path, text in patched.items():
        path.write_text(text)
    print(f"[*] Applied edits to: {', '.join(p.name for p in patched)}")

    ok = False
    try:
        checks = [check_harness(r.harness_path, crash_file, expected_verdict) for r in results]
        ok = all(checks)
    finally:
        if not ok:
            for path, text in originals.items():
                path.write_text(text)
            print("[-] Validation failed. Original files restored.")
        else:
            print(f"[+] All parsers now return {expected_verdict} on the triggering input.")

    return ok


# --------------------------------------------------------------- pipeline ---
def run_pipeline(
    results: List[ParserResult],
    file_format: str,
    crash_file_path: str,
    crash_offset: int = 0,
    provider: str = "gemini",
) -> PipelineResult:
    """Context -> LLM diagnosis + edits -> apply -> validate."""
    outcome = PipelineResult(provider=provider)

    # 1. Context: hex window + each parser's verdict and reason
    hex_dump = get_hex_window(crash_file_path, 0, window_size=1024)
    fuzzer_context = (
        f"CRASH CONTEXT:\nMutated data (hex window around offset {crash_offset}):\n"
        f"{hex_dump}\n\nPARSER RESULTS:\n"
    )
    for r in results:
        fuzzer_context += f"- {r.parser_name}: RESULT={r.verdict} | REASON={r.reason}\n"

    # 2. Ask the LLM
    prompt = generate_prompt(
        fuzzer_context=fuzzer_context,
        parser_sources=read_sources(results, "source_path"),
        harness_sources=read_sources(results, "harness_path"),
        file_format=file_format,
    )
    response = generate_patch(prompt, provider=provider)

    outcome.diagnosis = response.get("diagnosis", "")
    outcome.correct_verdict = response.get("correct_verdict")
    edits = response.get("edits") or []
    outcome.edits = edits if isinstance(edits, list) else []
    print(f"[*] LLM diagnosis: {outcome.diagnosis}")

    if not outcome.edits:
        print("[*] LLM proposed no usable edits.")
        return outcome
    if not isinstance(outcome.correct_verdict, bool):
        print("[-] LLM did not return a boolean 'correct_verdict'.")
        return outcome

    # 3. Apply + validate (rolls back on failure)
    parser_paths = {r.parser_name: str(r.source_path) for r in results}
    touched = sorted({
        parser_paths[e["parser"]]
        for e in outcome.edits
        if isinstance(e, dict) and e.get("parser") in parser_paths
    })

    ok = apply_and_validate(outcome.edits, results, crash_file_path, outcome.correct_verdict)
    (outcome.successful_patches if ok else outcome.failed_patches).extend(touched)
    return outcome