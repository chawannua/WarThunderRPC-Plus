"""Self-contained average-hash (aHash) implementation.

Reimplements the subset of the ``imagehash`` library's
``average_hash`` behaviour that this project needs, using only Pillow and
the standard library -- no numpy, no scipy, no imagehash at runtime.

The output is byte-for-byte identical to
``str(imagehash.average_hash(image))`` for the same input image, which
matters because the map hashes baked into ``wtrpc/maps.py`` were generated
with that library.

Algorithm (mirrors https://www.hackerfactor.com/blog/index.php?/archives/432-Looks-Like-It.html):
1. Convert the image to grayscale ("L").
2. Resize to (hash_size, hash_size) using LANCZOS resampling.
3. Compute the mean pixel value.
4. Set a bit for every pixel strictly greater than the mean.
5. Flatten the bits in row-major order and pack them into a lowercase hex
   string.
"""

from __future__ import annotations

from PIL import Image


def average_hash(image: Image.Image, hash_size: int = 8) -> str:
    """Compute the average hash of ``image`` as a lowercase hex string.

    Matches ``str(imagehash.average_hash(image, hash_size))`` exactly.
    """
    if hash_size < 2:
        raise ValueError("Hash size must be greater than or equal to 2")

    small = image.convert("L").resize((hash_size, hash_size), Image.LANCZOS)
    # "L" mode is 8-bit grayscale, so each byte of the raw buffer is one
    # pixel value (0-255) in row-major order -- equivalent to getdata() but
    # without Pillow's getdata() deprecation warning.
    pixels = list(small.tobytes())

    avg = sum(pixels) / len(pixels)

    bit_string = "".join("1" if p > avg else "0" for p in pixels)
    width = -(-len(bit_string) // 4)  # ceil division
    return format(int(bit_string, 2), f"0{width}x")


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Return the number of differing bits between two hex hash strings."""
    int_a = int(hash_a, 16)
    int_b = int(hash_b, 16)
    return bin(int_a ^ int_b).count("1")
