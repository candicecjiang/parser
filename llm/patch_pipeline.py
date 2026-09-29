import os
import re
import binascii
import shutil
from typing import List, Dict
from dataclasses import dataclass, field
from .llm_clients import generate_prompt, generate_patch
from .patch_validator import validate_patch


@dataclass
class ParserResult:
    parser_name: str
    verdict: bool
    reason: str
    source_path: str


@dataclass
class PipelineResult:
    successful_patches: List[str] = field(default_factory=list)
    failed_patches: List[str] = field(default_factory=list)
    diagnosis: str = ""
    provider: str = ""


def parse_stdout_line(parser_name: str, source_path: str, raw_output: str) -> ParserResult:
    match = re.search(r"RESULT:\s*(True|False)\s*\|\s*REASON:\s*(.*)", raw_output)
    if not match:
        raise ValueError(f"Could not parse harness output: {raw_output!r}")
    verdict = match.group(1) == "True"
    reason = match.group(2).strip()
    return ParserResult(parser_name=parser_name, verdict=verdict, reason=reason, source_path=source_path)


def resolve_source_path(parser_name: str, source_path: str) -> str:
    """Kaitai parsers are patched at the .ksy schema level, not the generated .py file."""
    if parser_name.lower() == "kaitai":
        return source_path.replace("png_ks.py", "png.ksy")
    return source_path


def build_parser_sources(results: List[ParserResult]) -> Dict[str, str]:
    """Reads the source code or declarative schema for every parser involved."""
    sources = {}
    for r in results:
        target_path = resolve_source_path(r.parser_name, r.source_path)
        try:
            with open(target_path, "r") as f:
                sources[r.parser_name] = f.read()
        except FileNotFoundError:
            sources[r.parser_name] = f"Error: Could not read {target_path}"
    return sources


def get_hex_window(file_path: str, offset: int = 0, window_size: int = 64) -> str:
    """Reads a binary file and returns a formatted hex dump around an offset."""
    try:
        with open(file_path, "rb") as f:
            start = max(0, offset - window_size)
            f.seek(start)
            chunk = f.read(window_size * 2)
        return binascii.hexlify(chunk, sep=b" ", bytes_per_sep=1).decode("ascii")
    except FileNotFoundError:
        return f"Error: Could not read crash file at {file_path}"


def save_failed_patch(target_path: str, patch_text: str, parser_name: str) -> str:
    """Saves a hallucinated/failed patch for later inspection."""
    failed_dir = os.path.join("results", "failed_patches")
    os.makedirs(failed_dir, exist_ok=True)
    base_name = os.path.basename(target_path)
    dest = os.path.join(failed_dir, f"{parser_name}_{base_name}")
    # avoid overwriting previous failed attempts for the same parser
    counter = 1
    while os.path.exists(dest):
        dest = os.path.join(failed_dir, f"{parser_name}_{base_name}.{counter}")
        counter += 1
    with open(dest, "w") as f:
        f.write(patch_text)
    return dest


def run_pipeline(
    results: List[ParserResult],
    file_format: str,
    crash_file_path: str,
    crash_offset: int = 0,
    provider: str = "gemini",
) -> PipelineResult:
    """Full pipeline: ingest context -> LLM classification/patching -> apply -> validate."""

    pipeline_result = PipelineResult(provider=provider)

    # 1. Gather context (Hex dump + Parser results)
    hex_dump = get_hex_window(crash_file_path, crash_offset)

    fuzzer_context = "CRASH CONTEXT:\n"
    fuzzer_context += f"Mutated Data (Hex Window around offset {crash_offset}):\n{hex_dump}\n\n"

    fuzzer_context += "PARSER RESULTS:\n"
    for r in results:
        fuzzer_context += f"- {r.parser_name}: RESULT={r.verdict} | REASON={r.reason}\n"

    # 2. Load the source files
    parser_sources = build_parser_sources(results)

    # 3. Generate the LLM prompt and call the LLM
    prompt = generate_prompt(
        fuzzer_context=fuzzer_context,
        parser_sources=parser_sources,
        file_format=file_format
    )

    response_dict = generate_patch(prompt, provider=provider)
    diagnosis = response_dict.get("diagnosis", "")
    pipeline_result.diagnosis = diagnosis
    print(f"[*] LLM Diagnosis: {diagnosis}")

    # 4. Extract, apply, and validate patches
    incorrect_parsers = response_dict.get("incorrect_parsers", [])
    patches = response_dict.get("patches", {})

    for parser_name in incorrect_parsers:
        if parser_name not in patches:
            print(f"[!] LLM flagged {parser_name} as incorrect but provided no patch")
            continue

        result_obj = next((r for r in results if r.parser_name == parser_name), None)
        if not result_obj:
            print(f"[!] LLM referenced unknown parser {parser_name!r}, skipping")
            continue

        target_path = resolve_source_path(parser_name, result_obj.source_path)
        patch_text = patches[parser_name]
        backup_path = target_path + ".bak"

        # Create backup
        shutil.copy2(target_path, backup_path)

        # Apply patch
        with open(target_path, "w") as f:
            f.write(patch_text)
        print(f"[*] Applied LLM patch to {parser_name} at {target_path}")

        # 5. Validate the patch
        is_valid = validate_patch(target_path, crash_file_path)

        if is_valid:
            print(f"[+] Patch validated successfully for {parser_name}.")
            pipeline_result.successful_patches.append(target_path)
        else:
            print(f"[-] Patch validation failed for {parser_name}. Restoring backup.")
            failed_path = save_failed_patch(target_path, patch_text, parser_name)
            print(f"[*] Failed patch saved for inspection at {failed_path}")
            shutil.copy2(backup_path, target_path)
            pipeline_result.failed_patches.append(target_path)

        # Clean up the backup file regardless of outcome
        if os.path.exists(backup_path):
            os.remove(backup_path)

    return pipeline_result