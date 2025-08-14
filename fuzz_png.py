import sys
import os
from pathlib import Path
from fuzzingbook.MutationFuzzer import MutationFuzzer
from check_png import check_kaitai, check_pillow, check_pypng


def fuzz_folder(folder_path, num_mutations=10):
    folder = Path(folder_path)
    if not folder.is_dir():
        print(f"Error: {folder_path} is not a directory.")
        sys.exit(1)

    png_files = list(folder.glob("*.png"))
    if not png_files:
        print(f"No PNG files found in {folder_path}.")
        return

    for png_file in png_files:
        with open(png_file, "rb") as f:
            seed = f.read()

        fuzzer = MutationFuzzer([seed])

        for i in range(num_mutations):
            mutated = fuzzer.fuzz()
            results = [
                check_kaitai(mutated),
                check_pillow(mutated),
                check_pypng(mutated)
            ]
            if results.count(results[0]) != len(results):
                print(f"Mutation {i}: Disagreement! Results: Kaitai={results[0]}, Pillow={results[1]}, PyPNG={results[2]}")


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <png-folder>")
        sys.exit(1)

    folder_path = sys.argv[1]
    fuzz_folder(folder_path)


if __name__ == "__main__":
    main()
