from google import genai
import os
from dotenv import load_dotenv

load_dotenv()

def generate_prompt(fuzzer_context, target_file_path, file_format, strict_parser, target_parser):
    # Read the source code
    with open(target_file_path, "r") as f:
        source_code = f.read()

    prompt = f"""
    You are an expert software engineer and security researcher.
    I am running a differential fuzzing campaign on {file_format} parsers. 
    
    During fuzzing, a malformed {file_format} file caused a discrepancy. The reference parser ({strict_parser}) correctly handled the file or rejected it safely, throwing this error:
    
    FUZZER EVIDENCE / STRICT PARSER ERROR:
    {fuzzer_context}
    
    However, the target parser ({target_parser}) failed to implement this validation, behaved unexpectedly, or crashed.
    
    TASK:
    Analyze the fuzzer evidence above. Please write a patch for the provided {target_parser} source code to properly implement these missing checks and handle the malformed data securely, mirroring the strictness of the reference parser.
    
    Please provide the corrected code. Include comments explaining the exact vulnerability or logic flaw you patched.
    """
    return prompt

def patch_with_gemini(prompt):
    print("[*] Asking Gemini 3.0 Pro for a patch...")
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model="gemini-3-pro-preview",
        contents=[prompt]
    )
    return response.text

# def patch_with_openai(prompt): ...
# def patch_with_anthropic(prompt): ...
