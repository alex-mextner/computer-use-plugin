"""Read-only lookup in mcp-auth-proxy's OAuth store: which client an issued access token belongs to.

WHY: the shim's own table (clients.py) learns a token only by watching its /.idp/token exchange, so a token issued before the
shim's table existed (a cutover, a lost state dir, an evicted entry) read `client=unverified` until the client's next exchange,
up to a day. The proxy itself keeps every issued access token with the client it was issued to, so the shim asks that store.
WHAT: mcp-auth-proxy (sigbit/mcp-auth-proxy v2, `repository-backend local`) keeps its fosite storage in a bbolt file `db` in its
data dir, bucket `mcp-oauth-proxy`, JSON values. An access token is stored under `access_token-<signature>`, where the signature
is the third segment of the JWT (fosite's DefaultJWTStrategy); the value holds the client with its redirect URIs. A registered
client is stored under `client-<id>`.
INVARIANTS: never takes the bbolt file lock and never writes: the running proxy holds an exclusive flock and a reader that
waited for it would hang the label, while a stray write could corrupt the proxy's state. A lookup descends the B+tree to the two
keys it needs under the newest meta page whose checksum is valid (either of the two, as bbolt itself recovers). It reads with
pread, element by element, never a whole page: a binary search reads each probed element header and at most one byte more of
its key than the key sought, and only the matching value is read, if it is no bigger than MAX_VALUE_BYTES. Registration is open
to anyone, so a stranger's multi-MiB record can share a leaf with ChatGPT's token; it is never read, it cannot hide the token,
and one lookup reads a few KiB whatever the store holds. bbolt is copy-on-write: the pages of transaction T's tree are freed
only by T+1 and reused from T+2 on, so if the newest meta is still T after the lookup, every page read belonged to T's tree; if
a commit landed meanwhile the lookup is repeated. Every page is checked (its own id, its kind) and every element read lies inside
its page; anything inconsistent gives no answer, which keeps the line `unverified`. Only the client id and its redirect URIs
leave this module; tokens, secrets and session data are never kept.
Format reference: go.etcd.io/bbolt v1.4 internal/common (page.go, meta.go, bucket.go), db.go (meta recovery).
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

from computerlib.targets.chatgpt.shim.jsonish import as_list, as_object, parse_object

BUCKET = b"mcp-oauth-proxy"
MAGIC = 0xED0CDAED
VERSION = 2
PAGE_HEADER = struct.Struct("<QHHI")  # id, flags, count, overflow
META = struct.Struct("<IIII QQ QQQ Q")  # magic, version, page size, flags, root (pgid, sequence), freelist, pgid, txid, checksum
BRANCH_ELEMENT = struct.Struct("<IIQ")  # pos, ksize, pgid
LEAF_ELEMENT = struct.Struct("<IIII")  # flags, pos, ksize, vsize
BUCKET_HEADER = struct.Struct("<QQ")  # root pgid (0 = inline), sequence
BRANCH, LEAF, META_PAGE = 0x01, 0x02, 0x04
BUCKET_LEAF = 0x01
PAGE_SIZES = (4096, 8192, 16384, 32768, 65536)  # where the second meta page may sit when the first cannot say
MAX_VALUE_BYTES = 256 * 1024  # a token or client record is a few KiB; a bigger value is never read
MAX_DEPTH = 16
ATTEMPTS = 3


class Inconsistent(Exception):
    """The pages read do not form a consistent bbolt tree (a concurrent commit, a damaged or foreign file)."""


@dataclass(frozen=True)
class IssuedClient:
    client_id: str
    redirect_uris: tuple[str, ...]


@dataclass(frozen=True)
class _Meta:
    page_size: int
    root: int
    txid: int


def _fnv64a(data: bytes) -> int:
    value = 0xCBF29CE484222325
    for byte in data:
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def _meta_at(fd: int, offset: int) -> _Meta | None:
    raw = os.pread(fd, PAGE_HEADER.size + META.size, offset)
    if len(raw) < PAGE_HEADER.size + META.size:
        return None
    _, flags, _, _ = PAGE_HEADER.unpack_from(raw, 0)
    body = raw[PAGE_HEADER.size :]
    magic, version, page_size, _, root, _, _, _, txid, checksum = META.unpack_from(body, 0)
    if flags != META_PAGE or magic != MAGIC or version != VERSION or checksum != _fnv64a(body[: META.size - 8]):
        return None
    if page_size not in PAGE_SIZES:
        return None
    return _Meta(page_size, root, txid)


def _newest_meta(fd: int) -> _Meta:
    """The newest valid meta page; the second is found at the first one's page size, or at each possible size without it."""
    first = _meta_at(fd, 0)
    sizes = (first.page_size,) if first is not None else PAGE_SIZES
    candidates = [first] + [_meta_at(fd, size) for size in sizes]
    valid = [m for m in candidates if m is not None and (first is None or m.page_size == first.page_size)]
    if not valid:
        raise Inconsistent("no valid meta page")
    return max(valid, key=lambda m: m.txid)


@dataclass(frozen=True)
class _Page:
    """A page read on demand, never whole: from the file at `start`, or from an inline bucket's bytes."""

    fd: int
    start: int
    length: int  # with its overflow pages; every read stays inside it
    flags: int
    count: int
    inline: bytes | None = None

    def read(self, offset: int, size: int) -> bytes:
        if offset < PAGE_HEADER.size or offset + size > self.length:
            raise Inconsistent("element out of page")
        data = os.pread(self.fd, size, self.start + offset) if self.inline is None else self.inline[offset : offset + size]
        if len(data) != size:
            raise Inconsistent("page beyond the end of the file")
        return data


class _Reader:
    def __init__(self, fd: int, meta: _Meta) -> None:
        self.fd = fd
        self.meta = meta

    def page(self, pgid: int) -> _Page:
        """Page `pgid` by its header alone, id-checked; its elements are read when a search probes them."""
        size = self.meta.page_size
        if pgid < 2:
            raise Inconsistent("page id out of range")
        head = os.pread(self.fd, PAGE_HEADER.size, pgid * size)
        if len(head) < PAGE_HEADER.size:
            raise Inconsistent("page beyond the end of the file")
        page_id, flags, count, overflow = PAGE_HEADER.unpack(head)
        if page_id != pgid:
            raise Inconsistent("page does not identify as itself")
        return _Page(self.fd, pgid * size, (overflow + 1) * size, flags, count)

    def get(self, page: _Page, key: bytes, depth: int = 0) -> tuple[bytes, int] | None:
        """(value, flags) of `key` in the subtree under `page`, or None when it is absent."""
        if depth > MAX_DEPTH:
            raise Inconsistent("tree too deep")
        if page.flags == BRANCH:
            child = _branch_child(page, key)
            return None if child is None else self.get(self.page(child), key, depth + 1)
        if page.flags != LEAF:
            raise Inconsistent("neither a leaf nor a branch page")
        return _leaf_value(page, key)

    def bucket(self, name: bytes) -> _Page | None:
        """The bucket's root page; an inline bucket's page sits inside its parent's value."""
        found = self.get(self.page(self.meta.root), name)
        if found is None or not found[1] & BUCKET_LEAF:
            return None
        value = found[0]
        if len(value) < BUCKET_HEADER.size:
            raise Inconsistent("short bucket header")
        root, _ = BUCKET_HEADER.unpack_from(value, 0)
        if root:
            return self.page(root)
        inline = value[BUCKET_HEADER.size :]
        if len(inline) < PAGE_HEADER.size:
            raise Inconsistent("short inline bucket")
        _, flags, count, _ = PAGE_HEADER.unpack_from(inline, 0)
        return _Page(self.fd, 0, len(inline), flags, count, inline)


def _order(page: _Page, start: int, ksize: int, tail: int, key: bytes) -> int:
    """-1, 0 or 1 as the stored key at `start` sorts against `key`. One byte past `key`'s length decides the order as well as the
    whole stored key would, so no more is read; the key and the `tail` bytes after it (its value) must lie inside the page."""
    if start + ksize + tail > page.length:
        raise Inconsistent("element out of page")
    stored = page.read(start, min(ksize, len(key) + 1))
    return (stored > key) - (stored < key)


def _branch_child(page: _Page, key: bytes) -> int | None:
    """The page id of the child that holds `key`: the last element whose first key is not past it, else the first (as bbolt)."""
    if page.count == 0:
        return None
    low, high = 0, page.count
    while low < high:  # the first element whose key is past `key`
        middle = (low + high) // 2
        at = PAGE_HEADER.size + middle * BRANCH_ELEMENT.size
        pos, ksize, _ = BRANCH_ELEMENT.unpack(page.read(at, BRANCH_ELEMENT.size))
        if _order(page, at + pos, ksize, 0, key) <= 0:
            low = middle + 1
        else:
            high = middle
    at = PAGE_HEADER.size + max(low - 1, 0) * BRANCH_ELEMENT.size
    _, _, child = BRANCH_ELEMENT.unpack(page.read(at, BRANCH_ELEMENT.size))
    return child


def _leaf_value(page: _Page, key: bytes) -> tuple[bytes, int] | None:
    low, high = 0, page.count
    while low < high:
        middle = (low + high) // 2
        at = PAGE_HEADER.size + middle * LEAF_ELEMENT.size
        flags, pos, ksize, vsize = LEAF_ELEMENT.unpack(page.read(at, LEAF_ELEMENT.size))
        order = _order(page, at + pos, ksize, vsize, key)
        if order == 0:
            if vsize > MAX_VALUE_BYTES:
                raise Inconsistent("value too big for a token or client record")
            return page.read(at + pos + ksize, vsize), flags
        low, high = (middle + 1, high) if order < 0 else (low, middle)
    return None


def _client(value: object) -> IssuedClient | None:
    record = as_object(value)
    client_id = record.get("ID")
    uris = as_list(record.get("RedirectURIs"))
    if not isinstance(client_id, str) or not uris:
        return None
    return IssuedClient(client_id, tuple(u for u in uris if isinstance(u, str)))


def _registered(reader: _Reader, root: _Page, issued: IssuedClient) -> IssuedClient:
    """The registration as it stands now wins over the copy the token carries; same id, by construction."""
    try:
        current = reader.get(root, f"client-{issued.client_id}".encode())
    except Inconsistent:
        # A registration past MAX_VALUE_BYTES or a damaged page: the copy the token carries is enough (the txid check in
        # client_of_token still guards the pages that were read).
        return issued
    registered = _client(parse_object(current[0])) if current is not None else None
    return registered if registered is not None and registered.client_id == issued.client_id else issued


def _lookup_once(fd: int, signature: str) -> tuple[IssuedClient | None, int]:
    meta = _newest_meta(fd)
    reader = _Reader(fd, meta)
    root = reader.bucket(BUCKET)
    if root is None:
        return None, meta.txid
    token = reader.get(root, f"access_token-{signature}".encode("ascii"))
    issued = _client(parse_object(token[0]).get("Client")) if token is not None and not token[1] & BUCKET_LEAF else None
    return (None if issued is None else _registered(reader, root, issued)), meta.txid


def client_of_token(path: Path, signature: str) -> IssuedClient | None:
    """The client the access token with this signature was issued to, or None: absent, unreadable, or never stable."""
    if not signature or not signature.isascii():
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        for _ in range(ATTEMPTS):
            try:
                client, txid = _lookup_once(fd, signature)
                if _newest_meta(fd).txid == txid:
                    return client
            except (Inconsistent, struct.error, OSError, UnicodeError):
                continue
        return None
    finally:
        os.close(fd)


def token_signature(authorization: str) -> str:
    """The signature fosite keys a JWT access token by: its third segment."""
    scheme, _, token = authorization.partition(" ")
    parts = token.strip().split(".")
    return parts[2] if scheme.lower() == "bearer" and len(parts) == 3 else ""
