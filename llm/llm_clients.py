from google import genai
from openai import OpenAI
from anthropic import Anthropic
import os
import json
from dotenv import load_dotenv

load_dotenv()

def generate_prompt(fuzzer_context, parser_sources, file_format):
    sources_block = "\n\n".join(
        f"--- {name} source code ---\n{code}"
        for name, code in parser_sources.items()
    )

    prompt = f"""
        I am running a differential fuzzing campaign on {file_format} parsers. A malformed {file_format} file caused the following parsers to disagree on whether the file is valid:

        FUZZER EVIDENCE:
        {fuzzer_context}

        Here is the source code for each parser involved:
        {sources_block}

        TASK:
        1. Determine which parser(s) behaved INCORRECTLY given the {file_format} specification.
        2. For each incorrect parser, write a patch that fixes the flaw and mirrors correct behavior.

        Respond strictly in JSON format matching this structure:
        {{
            "diagnosis": "<1-3 sentence explanation>",
            "incorrect_parsers": ["<parser_name>"],
            "patches": {{
                "<parser_name>": "<patched source code>"
            }}
        }}
    """

    return prompt

def safe_json_parse(raw_text: str, provider: str) -> dict:
    # To catch invalid JSON responses caused by: not enough tokens, model hallucinations, or formatting issues
    try:
        return json.loads(raw_text)
    
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
    print("[*] Asking Anthropic (Claude Sonnet 4.6) to diagnose and patch...")
    
    # picks up ANTHROPIC_API_KEY from environment
    client = Anthropic()
    
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,  # Anthropic requires max_tokens to be specified
        system="You are an expert software engineer and security researcher specializing in parser security and the LangSec discipline. Output strictly raw JSON with no markdown formatting.",
        messages=[
            {
                "role": "user", 
                "content": prompt
            },
            {
                # Pre-filling the assistant response forces Claude to start writing JSON immediately, 
                # bypassing any conversational introductory text like "Here is the JSON:"
                "role": "assistant",
                "content": "{"
            }
        ]
    )
    
    # Re-attach the opening brace that we used to pre-fill the response
    raw_text = "{" + response.content[0].text
    return safe_json_parse(raw_text, "Anthropic")

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