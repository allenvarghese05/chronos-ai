# Minimal stub for feedparser 6.0.x (ships no type information). Covers only what
# chronos.content.fetchers uses: parse() over in-memory bytes. Entries are untyped
# dict-likes, so they are exposed as Any and narrowed by the callers.
from typing import Any

class FeedParserDict(dict[str, Any]):
    def __getattr__(self, name: str) -> Any: ...

def parse(
    url_file_stream_or_string: bytes | str,
    etag: str | None = ...,
    modified: str | None = ...,
    agent: str | None = ...,
    referrer: str | None = ...,
    handlers: list[Any] | None = ...,
    request_headers: dict[str, str] | None = ...,
    response_headers: dict[str, str] | None = ...,
    resolve_relative_uris: bool | None = ...,
    sanitize_html: bool | None = ...,
) -> FeedParserDict: ...
