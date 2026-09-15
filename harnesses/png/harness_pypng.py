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
        return True

    except Exception as e:
        return (False, f"{e}")
    
result = check_pypng(data)

if result == True:
    print(result)
else:
    print(False)
    error = result[1]
    sys.stderr.write(f"Pypng error: {error}\n")
