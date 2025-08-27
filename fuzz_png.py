import sys
import os
from pathlib import Path
from fuzzingbook.MutationFuzzer import MutationFuzzer
from check_png import check_kaitai, check_pillow, check_pypng
import shutil

def fuzz_folder(folder_path, num_mutations=10):
    folder = Path(folder_path)
    if not folder.is_dir():
        print(f"Error: {folder_path} is not a directory.")
        sys.exit(1)

    png_files = list(folder.glob("*.png"))
    if not png_files:
        print(f"No PNG files found in {folder_path}.")
        return

    out_dir = Path("disagreed")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir()

    for png_file in png_files:
        with open(png_file, "rb") as f:
            seed_bytes = f.read()

        # Convert bytes to latin1 string for MutationFuzzer
        seed_str = seed_bytes.decode('latin1')
        fuzzer = MutationFuzzer([seed_str])

        # for i in range(num_mutations):
        count = 0
        while True:
            count += 1
            mutated_str = fuzzer.fuzz()
            mutated_bytes = mutated_str.encode('latin1')  # back to bytes

            results = [
                check_kaitai(mutated_bytes),
                check_pillow(mutated_bytes),
                check_pypng(mutated_bytes)
            ]
            # Check for disagreement
            if results[0] != results[1] or results[0] != results[2] or results[1] != results[2]: 
                print(f"Mutation {count}: Disagreement! Results: Kaitai={results[0]}, Pillow={results[1]}, PyPNG={results[2]}")
                
                out_path = out_dir / f"{png_file.stem}_mut{count}.png"
                with open(out_path, "wb") as out:
                    out.write(mutated_bytes)

def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <png-folder>")
        sys.exit(1)

    folder_path = sys.argv[1]
    fuzz_folder(folder_path)


if __name__ == "__main__":
    main()
