import math
import hashlib
import json
import os
import struct
import tempfile

from pathlib import Path


MAGIC = b"EAGLEBF1"


def save_bloom(bloom, path):

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    metadata = {
        "capacity": bloom.capacity,
        "error_rate": bloom.error_rate
    }

    header = json.dumps(metadata).encode("utf-8")

    temporary = None

    try:

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            delete=False
        ) as file:

            temporary = Path(file.name)

            file.write(MAGIC)
            # '>I' -> bid endian 32 bits unsigned interger
            file.write(struct.pack(">I", len(header)))
            file.write(header)
            file.write(bloom.bits)

            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary, path)

    finally:

        if temporary is not None:
            temporary.unlink(missing_ok=True)

def load_bloom(path):

    with open(path, "rb") as file:

        if file.read(len(MAGIC)) != MAGIC:
            raise ValueError("Invalid Bloom filter file")

        length_data = file.read(4)

        if len(length_data) != 4:
            raise ValueError("Incomplete Bloom filter header")

        header_length = struct.unpack(">I", length_data)[0]

        if header_length > 4096:
            raise ValueError("Invalid Bloom metadata size")

        header = file.read(header_length)

        if len(header) != header_length:
            raise ValueError("Incomplete Bloom metadata")

        metadata = json.loads(header)

        bloom = BloomFilter(
            capacity=metadata["capacity"],
            error_rate=metadata["error_rate"]
        )

        expected_size = len(bloom.bits)

        if file.readinto(bloom.bits) != expected_size:
            raise ValueError("Incomplete Bloom bit array")

        if file.read(1):
            raise ValueError("Unexpected data after Bloom bit array")

    return bloom


class BloomFilter:

    def __init__(self, capacity, error_rate=0.01):

        if capacity < 1 or not 0 < error_rate < 1:
            raise ValueError("Invalid Bloom filter configuration")

        self.size = math.ceil(
            -capacity * math.log(error_rate) / (math.log(2) ** 2)
        )

        self.hash_count = max(
            1,
            round((self.size / capacity) * math.log(2))
        )

        self.bits = bytearray((self.size + 7) // 8)

        self.capacity = capacity
        self.error_rate = error_rate

    def _hashes(self, url):

        digest = hashlib.blake2b(
            url.encode("utf-8"),
            digest_size=16
        ).digest()

        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big")

        for i in range(self.hash_count):
            yield (h1 + i * h2) % self.size

    def add(self, url):

        for index in self._hashes(url):
            self.bits[index // 8] |= 1 << (index % 8)

    def __contains__(self, url):

        return all(
            self.bits[index // 8] & (1 << (index % 8))
            for index in self._hashes(url)
        )



