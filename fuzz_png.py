from fuzzingbook.MutationFuzzer import MutationFuzzer
import subprocess

# Read the original PNG file as bytes
with open("test.png", "rb") as f:
    seed = f.read()

# Create a fuzzer with the PNG as the seed
fuzzer = MutationFuzzer([seed])

# Try 10 mutations
for i in range(10):  
    mutated = fuzzer.fuzz()
    fname = f"mutated_{i}.png"
    with open(fname, "wb") as out:
        out.write(mutated)
    # Run your png.py checker on the mutated file
    result = subprocess.run(["python3", "png.py", fname], capture_output=True, text=True)
    print(f"Test {i}: {result.stdout.strip()}")