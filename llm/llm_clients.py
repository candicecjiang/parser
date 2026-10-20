from google import genai
from openai import OpenAI
from anthropic import Anthropic
import os
import json
from dotenv import load_dotenv

load_dotenv()

def generate_prompt(fuzzer_context, parser_sources, harness_sources, file_format):
    parsers_block = "\n\n".join(
        f"=== PARSER SOURCE: {name} (patchable) ===\n{code}"
        for name, code in parser_sources.items()
    )
    harness_block = "\n\n".join(
        f"=== HARNESS: {name} (read-only) ===\n{code}"
        for name, code in harness_sources.items()
    )

    return f"""
        I am running a differential fuzzing campaign on {file_format} parsers. A malformed
        {file_format} file made the parsers disagree on whether it is valid.

        FUZZER EVIDENCE:
        {fuzzer_context}

        Each parser is run through a small harness that prints "RESULT: True" if the parser
        accepted the file and "RESULT: False" if it raised an error. The harnesses are shown
        only so you understand how each parser is invoked. They are READ-ONLY.

        {harness_block}

        {parsers_block}

        TASK:
        1. Using the {file_format} specification, decide whether this file should be ACCEPTED
        or REJECTED.
        2. For every parser whose behavior is wrong, propose the smallest edit to that
        parser's own source that fixes it.

        RULES:
        - Edit PARSER SOURCE only. Never edit harness code, and never add checks that only
        work around the harness. The fix must live in the parser's own code path.
        - "old" must be copied exactly (whitespace and indentation included) from the parser
        source above and must appear exactly once in that file. Include enough neighboring
        lines to make it unique.
        - Make the smallest change possible: add or correct the missing check. Do not
        rewrite, rename, reformat, or delete unrelated code, and keep every existing class,
        function, and attribute.
        - The edit must not change behavior on valid {file_format} files.
        - Parsers that already behave correctly need no edit. If no parser needs a change, or
        the specification is ambiguous, return "edits": [] and explain in "diagnosis".
        - A parser that fails with a Python error such as AttributeError or NameError (not a
        format error) has broken code. Fix it by restoring the missing definition.
        - Reason from the code shown and the specification, not from assumptions about code
        you cannot see.

        Respond with JSON only:
            {{
            "diagnosis": "<1-3 sentences>",
            "correct_verdict": <true if the file should be accepted, false if rejected>,
            "edits": [
                {{
                "parser": "<parser_name>",
                "old": "<a SHORT snippet, copied verbatim from the source>",
                "new": "<what that snippet should be replaced with>"
                }}
            ]
            }}

            Each "old" is only the lines you are changing plus just enough neighboring context
            to make it unique. Never include the whole file.
    """

def safe_json_parse(raw_text: str, provider: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):                       # strip markdown fences
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")     # drop any prose around the object
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[!] {provider} returned invalid JSON: {e}")
        print(f"[!] Raw response: {raw_text[:500]}")
        raise

def patch_with_gemini(prompt):
    print("[*] Asking Gemini to diagnose and patch...")
    
    # pick up GEMINI_API_KEY from the environment file
    client = genai.Client()
    
    response = client.models.generate_content(
        model="gemini-3.1-pro-preview", 
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction="You are an expert software engineer and security researcher specializing in parser security and the LangSec discipline.",
            response_mime_type="application/json"
        )
    )
    
    # Directly parse the guaranteed JSON response
    return safe_json_parse(response.text, "Gemini")

def patch_with_openai(prompt: str) -> dict:
    print("[*] Asking OpenAI (GPT-4o) to diagnose and patch...")
    
    # Automatically picks up OPENAI_API_KEY from the environment
    client = OpenAI()
    
    response = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system", 
                "content": "You are an expert software engineer and security researcher specializing in parser security and the LangSec discipline. Always output your response in raw JSON format."
            },
            {
                "role": "user", 
                "content": prompt
            }
        ]
    )
    
    return safe_json_parse(response.choices[0].message.content, "OpenAI")


def patch_with_anthropic(prompt: str) -> dict:
    print("[*] Asking Anthropic (Claude Sonnet 5.5) to diagnose and patch...")
    client = Anthropic()
    response = client.messages.create(
        model="claude-sonnet-5-5",
        max_tokens=16000,
        system="You are an expert software engineer and security researcher specializing in parser security and the LangSec discipline. Output a single JSON object only, with no markdown fences and no text before or after it.",
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError("Anthropic hit max_tokens before finishing the JSON; raise max_tokens")

    text = "".join(block.text for block in response.content if block.type == "text")
    return safe_json_parse(text, "Anthropic")

PROVIDERS = {
    "gemini": patch_with_gemini,
    "openai": patch_with_openai,
    "anthropic": patch_with_anthropic,
}


def generate_patch(prompt: str, provider: str = "gemini") -> dict:
    """
    Dispatches to the specified LLM provider and returns the parsed patch response.

    provider: one of "gemini", "openai", "anthropic"
    Returns: dict with keys "diagnosis", "incorrect_parsers", "patches"
    """
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider!r}. Must be one of {list(PROVIDERS)}")

    return PROVIDERS[provider](prompt)

def generate_patch_all_providers(prompt: str) -> dict:
    """Runs the prompt against all providers, returns {provider_name: result}."""
    results = {}
    for name, fn in PROVIDERS.items():
        try:
            results[name] = fn(prompt)
        except Exception as e:
            results[name] = {"error": str(e)}
    return results