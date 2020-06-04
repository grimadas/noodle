from hashlib import sha256, sha512

from ....util import int2byte
from typing import Union


def to_ascii(value: Union[str, bytes]) -> bytes:
    if isinstance(value, str):
        return b"".join(int2byte(ord(c)) for c in value)
    return value


def sha512_as_int(value):
    """
    Convert a SHA512 hash to an integer.
    """
    out = 0
    hashed = sha512(to_ascii(value)).digest()
    for i in range(len(hashed)):
        out <<= 8
        out |= ord(hashed[i : i + 1])
    return out


def sha256_as_int(value: Union[str, bytes]) -> int:
    """
    Convert a SHA256 hash to an integer.
    """
    out = 0
    hashed = sha256(to_ascii(value)).digest()
    for i in range(len(hashed)):
        out <<= 8
        out |= ord(hashed[i : i + 1])
    return out


def sha256_4_as_int(value: bytes) -> int:
    """
    Convert a SHA256 4 byte hash to an integer.
    """
    out = 0
    hashed = sha256(to_ascii(value)).digest()[:4]
    for i in range(len(hashed)):
        out <<= 8
        out |= ord(hashed[i : i + 1])
    return out