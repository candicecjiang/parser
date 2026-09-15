1. Rearranging file structure
- update input and results folder path in main.rs

2. Kaitai struct
- use Kaitai Struct Compiler 0.11 instead of 0.10
- the new version requires icc_4 to be b/x04 and rejects the previous seed file. Switched to a 1x1 pixel new seed.

3. Dockerfile
- update to match the above changes
- include icc_4.ksy and exif.ksy for kaitai struct 0.11
