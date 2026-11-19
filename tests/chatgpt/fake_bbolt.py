"""Writes small bbolt files in the layout mcp-auth-proxy's `db` has (go.etcd.io/bbolt v1.4), for the shim's store reader.

Layouts: `leaf` (every entry in the bucket's one leaf page, however big), `inline` (the bucket stored inside its parent's value,
as bbolt does for a small bucket), `branch` (a branch page over two leaf pages). A value bigger than a page spills into overflow
pages as in bbolt. Only what the reader needs: no freelist contents, one transaction.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

from computerlib.targets.chatgpt.shim.oauthdb import (
    BRANCH,
    BRANCH_ELEMENT,
    BUCKET,
    BUCKET_HEADER,
    BUCKET_LEAF,
    LEAF,
    LEAF_ELEMENT,
    MAGIC,
    META,
    META_PAGE,
    PAGE_HEADER,
    VERSION,
)

PAGE = 4096
FREELIST = 0x10
Item = tuple[bytes, bytes, int]


def _fnv64a(data: bytes) -> int:
    value = 0xCBF29CE484222325
    for byte in data:
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def _leaf_body(items: list[Item]) -> bytes:
    """Element headers then keys and values; each pos is relative to its own element header."""
    headers = b""
    data = b""
    table = len(items) * LEAF_ELEMENT.size
    for index, (key, value, flags) in enumerate(items):
        pos = table - index * LEAF_ELEMENT.size + len(data)
        headers += LEAF_ELEMENT.pack(flags, pos, len(key), len(value))
        data += key + value
    return headers + data


def _page(pgid: int, flags: int, count: int, body: bytes) -> bytes:
    size = PAGE_HEADER.size + len(body)
    pages = -(-size // PAGE)
    raw = PAGE_HEADER.pack(pgid, flags, count, pages - 1) + body
    return raw + b"\0" * (pages * PAGE - len(raw))


def _meta(pgid: int, root: int, high_water: int, txid: int, corrupt: bool) -> bytes:
    fields = META.pack(MAGIC, VERSION, PAGE, 0, root, 0, 2, high_water, txid, 0)[:-8]
    checksum = _fnv64a(fields) ^ (1 if corrupt else 0)
    return _page(pgid, META_PAGE, 0, fields + struct.pack("<Q", checksum))


def build(
    entries: dict[bytes, object],
    layout: str = "leaf",
    corrupt_metas: tuple[bool, bool] = (False, False),
) -> bytes:
    """The bytes of a bbolt file whose `mcp-oauth-proxy` bucket holds `entries` (values JSON-encoded unless already bytes)."""
    items: list[Item] = [
        (key, value if isinstance(value, bytes) else json.dumps(value).encode(), 0) for key, value in sorted(entries.items())
    ]
    pages: list[bytes] = []
    next_id = 4  # 0, 1 meta; 2 freelist; 3 root bucket

    def place(flags: int, count: int, body: bytes) -> int:
        nonlocal next_id
        pgid = next_id
        page = _page(pgid, flags, count, body)
        pages.append(page)
        next_id += len(page) // PAGE
        return pgid

    if layout == "inline":
        inline = PAGE_HEADER.pack(0, LEAF, len(items), 0) + _leaf_body(items)
        bucket_value = BUCKET_HEADER.pack(0, 0) + inline
    elif layout == "branch":
        half = len(items) // 2
        left = place(LEAF, half, _leaf_body(items[:half]))
        right = place(LEAF, len(items) - half, _leaf_body(items[half:]))
        firsts = [items[0][0], items[half][0]]
        body = b""
        data = b""
        for index, (child, key) in enumerate(zip((left, right), firsts, strict=True)):
            pos = 2 * BRANCH_ELEMENT.size - index * BRANCH_ELEMENT.size + len(data)
            body += BRANCH_ELEMENT.pack(pos, len(key), child)
            data += key
        bucket_value = BUCKET_HEADER.pack(place(BRANCH, 2, body + data), 0)
    else:
        bucket_value = BUCKET_HEADER.pack(place(LEAF, len(items), _leaf_body(items)), 0)
    root = _page(3, LEAF, 1, _leaf_body([(BUCKET, bucket_value, BUCKET_LEAF)]))
    high_water = next_id
    return (
        _meta(0, 3, high_water, 1, corrupt_metas[0])
        + _meta(1, 3, high_water, 2, corrupt_metas[1])
        + _page(2, FREELIST, 0, b"")
        + root
        + b"".join(pages)
    )


def write(path: Path, entries: dict[bytes, object], layout: str = "leaf") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build(entries, layout))
    return path


def client_record(client_id: str, redirect: str) -> dict[str, object]:
    return {"ID": client_id, "Secret": "", "RedirectURIs": [redirect], "Public": True}


def token_record(client_id: str, redirect: str) -> dict[str, object]:
    """An access token session as mcp-auth-proxy stores it (models.Request): the client rides along with the session data."""
    return {"ID": "req", "Client": client_record(client_id, redirect), "SessionData": {"secret": "never-read"}}
