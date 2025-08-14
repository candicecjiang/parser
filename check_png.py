import sys
import io
from kaitaistruct import KaitaiStructError, KaitaiStream
from png_ks import Png  # Kaitai-generated
from PIL import Image, UnidentifiedImageError   # Pillow
import png as pypng   # PyPNG


def check_kaitai(file_bytes):
    try:
        png_obj = Png(KaitaiStream(io.BytesIO(file_bytes)))
        # Sanity check for IHDR chunk existence
        if not hasattr(png_obj, 'ihdr'):
            return False
        return True
    except Exception:
        return False

def check_pillow(file_bytes):
    try:
        with Image.open(io.BytesIO(file_bytes)) as img:
            img.load()  # Fully decode image to pixel data
        return True
    except Exception:
        return False

def check_pypng(file_bytes):
    try:
        reader = pypng.Reader(bytes=file_bytes)
        width, height, rows, info = reader.read()
        # Force reading all rows to ensure CRC checks & decompression happen
        for _ in rows:
            pass
        return True
    except Exception:
        return False


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <png-file>")
        sys.exit(1)

    filename = sys.argv[1]
    with open(filename, "rb") as f:
        data = f.read()

    results = {
        "Kaitai": check_kaitai(data),
        "Pillow": check_pillow(data),
        "PyPNG": check_pypng(data)
    }

    for parser, result in results.items():
        if result is True:
            print(f"{parser}: OK")
        else:
            print(f"{parser}: FAIL → {result}")


if __name__ == "__main__":
    main()
