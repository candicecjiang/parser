#!/usr/bin/env python3
import sys, os, io

if "AFL_DUMP_MAP_SIZE" in os.environ:
    print(65536)
    sys.exit(0)
    
import afl
from kaitaistruct import KaitaiStructError, KaitaiStream
from png_ks import Png


# 1. Handshake with the Rust Fuzzer
afl.init()

# 2. Read the raw bytes from the Fuzzer
data = sys.stdin.buffer.read()

def check_kaitai(file_bytes):
    try:
        png_obj = Png(KaitaiStream(io.BytesIO(file_bytes)))
        return True

    except Exception as e:
        return (False, f"{e}")
    
result = check_kaitai(data)

if result is True:
    print(result)
else:
    print(False)
    error = result[1]
    sys.stderr.write(f"Kaitai error: {error}\n")
