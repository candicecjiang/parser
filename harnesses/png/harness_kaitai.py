#!/usr/bin/env python3
import sys, os, io
import afl
from kaitaistruct import KaitaiStructError, KaitaiStream
from png_ks import Png

if "AFL_DUMP_MAP_SIZE" in os.environ:
    print(65536)
    sys.exit(0)
    
# Handshake with the Rust Fuzzer
afl.init()

# Read the raw bytes from the Fuzzer
data = sys.stdin.buffer.read()

def check_kaitai(file_bytes):
    try:
        png_obj = Png(KaitaiStream(io.BytesIO(file_bytes)))
        return True, "parsed successfully"

    except KaitaiStructError as e:
        return False, f"KaitaiStructError: {e}"
    except Exception as e:
        return False, f"UnexpectedError ({type(e).__name__}): {e}"

status, reason = check_kaitai(data)

print(f"RESULT: {status} | REASON: {reason}")