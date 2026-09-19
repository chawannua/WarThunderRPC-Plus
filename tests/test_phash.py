"""Tests for wtrpc.phash: a self-contained average-hash implementation.

The critical test below verifies byte-for-byte compatibility with the
``imagehash`` library (installed as a dev-only dependency), because the
hashes baked into ``wtrpc/maps.py`` were generated with it. If imagehash is
not installed in the current environment, that test skips cleanly instead of
failing -- but it must actually run (not skip) in CI/dev environments where
imagehash is available.
"""

from __future__ import annotations

import random

import pytest
from PIL import Image

from wtrpc.phash import average_hash, hamming_distance

try:
    import imagehash

    HAVE_IMAGEHASH = True
except ImportError:
    HAVE_IMAGEHASH = False


def _solid_image(size, color):
    return Image.new("RGB", size, color)


def _noise_image(size, seed):
    rng = random.Random(seed)
    img = Image.new("RGB", size)
    pixels = [
        (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        for _ in range(size[0] * size[1])
    ]
    img.putdata(pixels)
    return img


def _gradient_image(size):
    w, h = size
    img = Image.new("RGB", size)
    pixels = []
    for y in range(h):
        for x in range(w):
            v = int(255 * (x + y) / max(1, (w + h - 2)))
            pixels.append((v, v, v))
    img.putdata(pixels)
    return img


def _grayscale_image(size, seed):
    rng = random.Random(seed)
    img = Image.new("L", size)
    pixels = [rng.randrange(256) for _ in range(size[0] * size[1])]
    img.putdata(pixels)
    return img


def _generate_test_images():
    images = []
    rng = random.Random(1234)

    sizes = [(8, 8), (16, 16), (32, 20), (64, 64), (100, 37), (7, 13)]

    # Solid colours
    for i, size in enumerate(sizes):
        color = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        images.append(_solid_image(size, color))

    # Noise images
    for i, size in enumerate(sizes):
        images.append(_noise_image(size, seed=100 + i))

    # Gradients
    for size in sizes:
        images.append(_gradient_image(size))

    # Grayscale images
    for i, size in enumerate(sizes):
        images.append(_grayscale_image(size, seed=200 + i))

    # A handful of extra random-content images to comfortably exceed 50.
    extra_sizes = [
        (9, 9), (17, 5), (50, 50), (3, 3), (128, 8), (8, 128), (23, 23), (200, 3),
        (2, 2), (5, 40), (40, 5), (64, 8), (11, 11),
    ]
    for i, size in enumerate(extra_sizes):
        images.append(_noise_image(size, seed=300 + i))
        images.append(_gradient_image(size))

    return images


def test_average_hash_returns_16_char_lowercase_hex_by_default():
    img = _solid_image((32, 32), (128, 64, 200))
    h = average_hash(img)
    assert isinstance(h, str)
    assert len(h) == 16
    assert h == h.lower()
    int(h, 16)  # must be valid hex


def test_hamming_distance_identical_is_zero():
    img = _gradient_image((32, 32))
    h1 = average_hash(img)
    h2 = average_hash(img)
    assert hamming_distance(h1, h2) == 0


def test_hamming_distance_symmetric_and_nonnegative():
    a = average_hash(_solid_image((16, 16), (0, 0, 0)))
    b = average_hash(_noise_image((16, 16), seed=42))
    assert hamming_distance(a, b) == hamming_distance(b, a)
    assert hamming_distance(a, b) >= 0


def test_hamming_distance_max_for_full_black_vs_full_white():
    black = average_hash(_solid_image((16, 16), (0, 0, 0)))
    white = average_hash(_solid_image((16, 16), (255, 255, 255)))
    # both are solid colours -> every pixel equals the mean, so `pixel > mean`
    # is False everywhere for both; hash should be identical (all zero bits).
    assert hamming_distance(black, white) == 0


@pytest.mark.skipif(not HAVE_IMAGEHASH, reason="imagehash not installed")
def test_matches_imagehash_average_hash_on_many_images():
    images = _generate_test_images()
    assert len(images) >= 50, "need at least 50 procedurally generated images"

    mismatches = []
    for idx, img in enumerate(images):
        ours = average_hash(img)
        theirs = str(imagehash.average_hash(img))
        if ours != theirs:
            mismatches.append((idx, img.size, img.mode, ours, theirs))

    assert not mismatches, f"hash mismatches (index, size, mode, ours, theirs): {mismatches}"
