"""Signed media URLs and byte-range serving (T5.1, docs/05 §3).

``GET /v1/media/{public_id}/{file}?exp=&sig=`` needs no bearer token (the player cannot set
headers): the HMAC-SHA256 signature over ``"{public_id}/{file}|{exp}"`` with the server secret
*is* the authorisation. URLs live 1 hour. Only a fixed set of file names can ever be served, so
path traversal is impossible by construction (anything else is a 404).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

URL_TTL_SEC = 3600
CHUNK = 64 * 1024
#: The only files a reel ever exposes, with their content types.
SERVABLE: dict[str, str] = {
    "reel.mp4": "video/mp4",
    "thumb.jpg": "image/jpeg",
    "captions.json": "application/json",
    "voice.mp3": "audio/mpeg",
}
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_PUBLIC_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")  # Crockford base32 ULID


def sign(secret: str, public_id: str, file: str, exp: int) -> str:
    message = f"{public_id}/{file}|{exp}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_url(base_url: str, secret: str, public_id: str, file: str, now_epoch: int) -> str:
    exp = now_epoch + URL_TTL_SEC
    return f"{base_url}/media/{public_id}/{file}?exp={exp}&sig={sign(secret, public_id, file, exp)}"


def valid_name(public_id: str, file: str) -> bool:
    return bool(_PUBLIC_ID.match(public_id)) and file in SERVABLE


def signature_ok(secret: str, public_id: str, file: str, exp: int, sig: str) -> bool:
    return hmac.compare_digest(sign(secret, public_id, file, exp), sig)


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int  # inclusive

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class RangeNotSatisfiable(ValueError):
    pass


def parse_range(header: str | None, size: int) -> ByteRange | None:
    """Single ``bytes=a-b`` / ``a-`` / ``-n`` range, or ``None`` for the whole file.

    Raises:
        RangeNotSatisfiable: malformed, multi-range, or outside the file.
    """
    if header is None:
        return None
    match = _RANGE.match(header.strip())
    if match is None or size == 0:
        raise RangeNotSatisfiable(header)
    first, last = match.groups()
    if first == "" and last == "":
        raise RangeNotSatisfiable(header)
    if first == "":
        n = int(last)
        if n == 0:
            raise RangeNotSatisfiable(header)
        return ByteRange(max(0, size - n), size - 1)
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise RangeNotSatisfiable(header)
    return ByteRange(start, end)


def iter_file(path: Path, start: int, length: int) -> Iterator[bytes]:
    with path.open("rb") as stream:
        stream.seek(start)
        remaining = length
        while remaining > 0:
            chunk = stream.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
