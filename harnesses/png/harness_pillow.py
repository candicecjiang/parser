#!/usr/bin/env python3
import sys, os, io
import afl
from PIL import Image

if "AFL_DUMP_MAP_SIZE" in os.environ:
    print(65536)
    sys.exit(0)

# Handshake with the Rust Fuzzer
afl.init()

def check_pillow(file_bytes):
    try:
        with Image.open(io.BytesIO(file_bytes)) as img:
            img.load()  # Fully decode image to pixel data
        return True, "parsed successfully"

    except Exception as e:
        return False, f"{e}"

# Read the raw bytes from the Fuzzer
data = sys.stdin.buffer.read()

status, reason = check_pillow(data)

print(f"RESULT: {status} | REASON: {reason}")
