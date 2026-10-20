## Overview

This project finds and patches **parser differentials**: inputs that different parsers of the same format (currently PNG) interpret differently. A differential fuzzer finds the disagreement, an LLM decides which parser is wrong and proposes a minimal fix, and the fix is validated automatically.

## How the pipeline works

1. **Fuzz.** The LibAFL-based differential fuzzer (`fuzzer/`) mutates seed files and runs each input through every parser harness. It compares only each harness's `RESULT: True/False` verdict, not the error text. On a disagreement it prints a `[DIFF]` block with every parser's full output and saves the input.
2. **Stop at the first bug.** `orchestrator.py` reads the fuzzer output, stops the fuzzer, and copies the triggering input into the run folder.
3. **Diagnose and patch.** `llm/patch_pipeline.py` sends the LLM each parser's verdict and reason, a hex dump of the input, the parser source (editable) and the harness source (read-only). The LLM returns JSON with a diagnosis, the correct verdict, and a list of small search-and-replace edits.
4. **Apply safely.** Each edit's `old` text must match exactly once and every edited file must still compile. Only then are the files written.
5. **Validate.** Every harness is re-run on the triggering input and must return the LLM's correct verdict. Otherwise the original files are restored.
6. **Re-fuzz.** After a successful patch the fuzzer restarts from the seeds. An incomplete fix or a regression shows up as the next round's bug.
7. **Stop** when no discrepancy is found within the timeout, a patch fails validation, or the round limit is reached.

## Project structure

- `orchestrator.py`: run loop (fuzz, patch, restart); manages baselines and run folders.
- `llm/llm_clients.py`: prompt template and wrappers for Gemini, OpenAI and Anthropic; `generate_patch(prompt, provider)` is the entry point.
- `llm/patch_pipeline.py`: builds the LLM context, applies edits with rollback, and validates patches.
- `fuzzer/src/main.rs`: differential fuzzer built on LibAFL (written by Ben).
- `fuzzer/config.toml`: maps each parser name to its harness script.
- `harnesses/png/harness_*.py`: one harness per parser. Each reads a file from stdin and prints `RESULT: <True|False> | REASON: <text>`.
- `harnesses/png/png.ksy`, `png_ks.py`: the Kaitai spec and the parser generated from it (`png_ks.py` is what the LLM edits). `icc_4` and `exif` are generated alongside, as Kaitai Struct 0.11 requires.
- `input/`: seed files for the fuzzer.
- `baselines/png/`: pristine copies of every parser the LLM can edit (Kaitai's generated file, Pillow's `PngImagePlugin.py`, pypng's `png.py`). Created by `--init-baselines`; each run restores from here. Not committed.
- `results/`: LibAFL scratch space, emptied at the start of every run. Not committed.
- `Dockerfile`, `entrypoint.sh`: the environment (AFL++, Rust nightly, a Python venv, Kaitai Struct compiler 0.11). The entrypoint activates the venv and opens a shell.
- `.env`: API keys (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). Not committed.

## Usage

```bash
docker build -t parser-test .
mkdir -p runs baselines
docker run --env-file .env \
  -v $(pwd)/runs:/app/runs \
  -v $(pwd)/baselines:/app/baselines \
  -it parser-test
```

Inside the container:

```bash
python orchestrator.py --init-baselines                     # once, while parsers are pristine
python orchestrator.py --provider anthropic                 # or gemini / openai
python orchestrator.py --provider gemini --max-rounds 5 --fuzz-timeout 1200
```

## Run output

Each run writes to `runs/<timestamp>_<provider>/`:

- `rounds.jsonl`: one record per bug, with each parser's verdict and reason, the LLM's diagnosis, the exact edits, and whether validation passed.
- `triggers/`: the fuzzed input that caused each discrepancy.
- `patches/*.diff`: unified diff of each changed parser against its baseline.
- `patched/`: full final copy of each changed parser (not committed).

## Adding a new parser or format

- Write a harness in `harnesses/<format>/` that reads stdin and prints the `RESULT: ... | REASON: ...` line.
- Add the parser to `fuzzer/config.toml` and to `PARSERS` in `orchestrator.py`.
- Add seed files to `input/`.

## Next steps
- Test with more seed files
- Run the fuzzer longer after the CRC bugs are fixed to find deeper bugs