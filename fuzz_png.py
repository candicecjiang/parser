from fuzzingbook.MutationFuzzer import MutationFuzzer
import subprocess

# Read the original PNG file as bytes
with open("test.png", "rb") as f:
    seed_bytes = f.read()

# Convert bytes to a string
seed_str = seed_bytes.decode('latin1')

# Create a fuzzer with the PNG as the seed
fuzzer = MutationFuzzer([seed_str])

# Try 10 mutations
for i in range(10):  
    mutated_str = fuzzer.fuzz()
    mutated_bytes = mutated_str.encode('latin1')

    fname = f"mutated_{i}.png"
    with open(fname, "wb") as out:
        out.write(mutated_bytes)
    # Run your png.py checker on the mutated file
    result = subprocess.run(["python3", "png.py", fname], capture_output=True, text=True)
    print(f"Test {i}: {result.stdout.strip()}")
