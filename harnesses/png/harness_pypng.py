#!/usr/bin/env python3
import sys, os
import afl
import png as pypng   # PyPNG

if "AFL_DUMP_MAP_SIZE" in os.environ:
    print(65536)
    sys.exit(0)

afl.init()

data = sys.stdin.buffer.read()

def check_pypng(file_bytes):
    try:
        reader = pypng.Reader(bytes=file_bytes)
        width, height, rows, info = reader.read()
        # Force reading all rows to ensure CRC checks & decompression happen
        for _ in rows:
            pass
        return True, "parsed successfully"

    except Exception as e:
        return False, f"{e}"
    
status, reason = check_pypng(data)

print(f"RESULT: {status} | REASON: {reason}")
