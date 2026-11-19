"""The proxy's OAuth store as the source of the verified label (#37): a ChatGPT token the shim never saw issued (a cutover,
a restart with a lost table, an evicted entry) is still attributed, while a forged or rejected token never is."""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
from pathlib import Path

import pytest

from computerlib.targets.chatgpt.shim import clients, oauthdb
from tests.chatgpt import fake_bbolt
from tests.chatgpt.shim_harness import Running, jwt, running_shim

OPENAI_UA = "openai-mcp/1.0.0"
CHATGPT = "cid-chatgpt"
LOCAL = "cid-local"
CHATGPT_REDIRECT = "https://chatgpt.com/connector/oauth/abc123"
LOCAL_REDIRECT = "http://localhost:9/cb"
SIG_CHATGPT = "c" * 342
SIG_CHATGPT_2 = "d" * 342
SIG_LOCAL = "l" * 342


def _entries(**extra: object) -> dict[bytes, object]:
    entries: dict[bytes, object] = {
        f"client-{CHATGPT}".encode(): fake_bbolt.client_record(CHATGPT, CHATGPT_REDIRECT),
        f"client-{LOCAL}".encode(): fake_bbolt.client_record(LOCAL, LOCAL_REDIRECT),
        f"access_token-{SIG_CHATGPT}".encode(): fake_bbolt.token_record(CHATGPT, CHATGPT_REDIRECT),
        f"access_token-{SIG_CHATGPT_2}".encode(): fake_bbolt.token_record(CHATGPT, CHATGPT_REDIRECT),
        f"access_token-{SIG_LOCAL}".encode(): fake_bbolt.token_record(LOCAL, LOCAL_REDIRECT),
        b"refresh_token-r1": {"ID": "x"},
    }
    entries.update({key.encode(): value for key, value in extra.items()})
    return entries


def _store(tmp_path: Path, layout: str = "leaf") -> Path:
    return fake_bbolt.write(tmp_path / "oauth" / "db", _entries(), layout)


def _mcp_call(shim: Running, token: str, status: int = 200) -> None:
    shim.upstream.routes["/mcp"] = (status, {"Content-Type": "application/json"}, b"{}")
    shim.request(
        "POST", "/mcp", headers={"Authorization": f"Bearer {token}", "User-Agent": OPENAI_UA}, body=b'{"method":"tools/list"}'
    )


def _labels(shim: Running, at_least: int) -> list[str]:
    return [next(part for part in line.split() if part.startswith("client=")) for line in shim.log_lines(at_least)]


@pytest.mark.parametrize("layout", ["leaf", "inline", "branch"])
def test_the_lookup_finds_the_client_of_a_token_in_every_bucket_layout(tmp_path: Path, layout: str) -> None:
    db = _store(tmp_path, layout)
    assert oauthdb.client_of_token(db, SIG_CHATGPT) == oauthdb.IssuedClient(CHATGPT, (CHATGPT_REDIRECT,))
    assert oauthdb.client_of_token(db, SIG_LOCAL) == oauthdb.IssuedClient(LOCAL, (LOCAL_REDIRECT,))
    assert oauthdb.client_of_token(db, "f" * 342) is None


@pytest.mark.parametrize("layout", ["leaf", "branch"])
def test_every_key_among_many_is_found_by_the_element_search(tmp_path: Path, layout: str) -> None:
    """The lookup bisects the sorted elements of each page; every key, the first and the last included, must be reachable."""
    tokens: dict[bytes, object] = {
        f"access_token-{n:03d}".encode(): fake_bbolt.token_record(f"cid-{n}", LOCAL_REDIRECT) for n in range(200)
    }
    db = fake_bbolt.write(tmp_path / "db", tokens, layout)
    for n in range(200):
        assert oauthdb.client_of_token(db, f"{n:03d}") == oauthdb.IssuedClient(f"cid-{n}", (LOCAL_REDIRECT,)), n
    for absent in ("200", "00", "0000", "/"):
        assert oauthdb.client_of_token(db, absent) is None, absent


def test_the_current_registration_wins_over_the_copy_the_token_carries(tmp_path: Path) -> None:
    entries = _entries()
    entries[f"access_token-{SIG_LOCAL}".encode()] = fake_bbolt.token_record(LOCAL, CHATGPT_REDIRECT)
    db = fake_bbolt.write(tmp_path / "db", entries)
    assert oauthdb.client_of_token(db, SIG_LOCAL) == oauthdb.IssuedClient(LOCAL, (LOCAL_REDIRECT,))


def test_a_value_bigger_than_a_page_is_read_across_its_overflow_pages(tmp_path: Path) -> None:
    big = fake_bbolt.token_record(CHATGPT, CHATGPT_REDIRECT) | {"SessionData": {"pad": "x" * 9000}}
    db = fake_bbolt.write(tmp_path / "db", {f"access_token-{SIG_CHATGPT}".encode(): big})
    assert oauthdb.client_of_token(db, SIG_CHATGPT) == oauthdb.IssuedClient(CHATGPT, (CHATGPT_REDIRECT,))


def test_a_store_bloated_by_open_registrations_still_answers(tmp_path: Path) -> None:
    """Registration is open to anyone and the proxy keeps any redirect URI: an oversized foreign record is never read."""
    entries = _entries()
    entries[b"client-zz-flood"] = fake_bbolt.client_record("zz-flood", "https://x/" + "a" * (3 * 1024 * 1024))
    db = fake_bbolt.write(tmp_path / "db", entries, "branch")
    assert db.stat().st_size > 3 * 1024 * 1024
    assert oauthdb.client_of_token(db, SIG_CHATGPT) == oauthdb.IssuedClient(CHATGPT, (CHATGPT_REDIRECT,))


FLOOD_BYTES = 4 * 1024 * 1024
LOOKUP_READ_BOUND = 64 * 1024  # what one lookup may read in all, whatever else the store holds


def _requested_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The byte count of every pread the lookup asks for: what it allocates, whatever the file returns."""
    requested: list[int] = []
    real = os.pread

    def counting(fd: int, length: int, offset: int) -> bytes:
        requested.append(length)
        return real(fd, length, offset)

    monkeypatch.setattr(os, "pread", counting)
    return requested


def test_a_token_sharing_a_leaf_with_a_multi_mib_registration_is_attributed_with_bounded_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#60: registration is open to anyone, so a stranger's multi-MiB client can land in the leaf that holds ChatGPT's token and
    registration; the lookup reads only the elements it probes and the values it needs, never the stranger's record."""
    entries = _entries()
    entries[f"access_token-{SIG_CHATGPT}".encode()] = fake_bbolt.token_record(CHATGPT, "https://chatgpt.com/connector/oauth/old")
    entries[b"client-zz-flood"] = fake_bbolt.client_record("zz-flood", "https://x/" + "a" * FLOOD_BYTES)
    db = fake_bbolt.write(tmp_path / "db", entries)
    _, _, count, overflow = oauthdb.PAGE_HEADER.unpack_from(db.read_bytes(), 4 * fake_bbolt.PAGE)
    assert count == len(entries) and (overflow + 1) * fake_bbolt.PAGE > FLOOD_BYTES  # one leaf holds them all
    requested = _requested_reads(monkeypatch)
    # The redirect URI comes from the current registration in that same leaf, not from the stale copy the token carries.
    assert oauthdb.client_of_token(db, SIG_CHATGPT) == oauthdb.IssuedClient(CHATGPT, (CHATGPT_REDIRECT,))
    assert sum(requested) < LOOKUP_READ_BOUND, requested
    requested.clear()
    assert oauthdb.client_of_token(db, "f" * 342) is None
    assert sum(requested) < LOOKUP_READ_BOUND, requested


def test_a_record_too_big_to_be_a_token_gives_no_answer_without_being_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge = fake_bbolt.token_record(CHATGPT, CHATGPT_REDIRECT) | {"SessionData": {"pad": "x" * FLOOD_BYTES}}
    db = fake_bbolt.write(tmp_path / "db", {f"access_token-{SIG_CHATGPT}".encode(): huge})
    requested = _requested_reads(monkeypatch)
    assert oauthdb.client_of_token(db, SIG_CHATGPT) is None
    assert sum(requested) < LOOKUP_READ_BOUND, requested


@pytest.mark.parametrize("corrupt", [(False, True), (True, False)], ids=["second-meta-damaged", "first-meta-damaged"])
def test_either_valid_meta_page_is_enough(tmp_path: Path, corrupt: tuple[bool, bool]) -> None:
    path = tmp_path / "db"
    path.write_bytes(fake_bbolt.build(_entries(), corrupt_metas=corrupt))
    assert oauthdb.client_of_token(path, SIG_CHATGPT) == oauthdb.IssuedClient(CHATGPT, (CHATGPT_REDIRECT,))


def test_a_damaged_or_foreign_file_gives_no_answer_instead_of_a_wrong_one(tmp_path: Path) -> None:
    both_metas = tmp_path / "metas"
    both_metas.write_bytes(fake_bbolt.build(_entries(), corrupt_metas=(True, True)))
    torn = bytearray(fake_bbolt.build(_entries()))
    torn[4 * fake_bbolt.PAGE] = 99  # the bucket's leaf page no longer identifies as page 4
    torn_path = tmp_path / "torn"
    torn_path.write_bytes(bytes(torn))
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"SQLite format 3\0" + b"\0" * 8192)
    for path in (both_metas, torn_path, foreign, tmp_path / "missing"):
        assert oauthdb.client_of_token(path, SIG_CHATGPT) is None, path.name


def test_a_commit_during_the_lookup_makes_it_start_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pages read under meta T are only trustworthy while T is still the newest meta (copy-on-write reuse from T+2 on)."""
    db = _store(tmp_path)
    real = oauthdb._newest_meta  # pyright: ignore[reportPrivateUsage]
    shifts = iter([0, 1, 0, 0])  # the first lookup starts at T and ends at T+1, as if the proxy committed meanwhile
    seen: list[int] = []

    def moving(fd: int) -> oauthdb._Meta:  # pyright: ignore[reportPrivateUsage]
        meta = dataclasses.replace(real(fd), txid=real(fd).txid + next(shifts))
        seen.append(meta.txid)
        return meta

    monkeypatch.setattr(oauthdb, "_newest_meta", moving)
    assert oauthdb.client_of_token(db, SIG_CHATGPT) == oauthdb.IssuedClient(CHATGPT, (CHATGPT_REDIRECT,))
    assert seen == [2, 3, 2, 2]

    always = itertools.count()

    def always_moving(fd: int) -> oauthdb._Meta:  # pyright: ignore[reportPrivateUsage]
        return dataclasses.replace(real(fd), txid=next(always))

    monkeypatch.setattr(oauthdb, "_newest_meta", always_moving)
    assert oauthdb.client_of_token(db, SIG_CHATGPT) is None


def test_a_chatgpt_token_issued_before_the_shim_existed_is_verified(tmp_path: Path) -> None:
    """The live case of #37: the shim starts with no table, ChatGPT keeps calling with the token it already holds."""
    with running_shim(tmp_path, oauth_db=_store(tmp_path)) as shim:
        _mcp_call(shim, jwt("jti-before", SIG_CHATGPT))
        assert _labels(shim, 1) == ["client=openai-mcp"]
    saved = json.loads((tmp_path / "state" / "clients.json").read_text())
    assert saved["exchanged"] == {CHATGPT: "openai"} and saved["tokens"] == {"jti-before": CHATGPT}


def test_two_tokens_of_the_same_client_right_after_a_restart_are_both_verified(tmp_path: Path) -> None:
    with running_shim(tmp_path, oauth_db=_store(tmp_path)) as shim:
        _mcp_call(shim, jwt("jti-a", SIG_CHATGPT))
        _mcp_call(shim, jwt("jti-b", SIG_CHATGPT_2))
        _mcp_call(shim, jwt("jti-b", SIG_CHATGPT_2))
        assert _labels(shim, 3) == ["client=openai-mcp"] * 3


def test_a_restart_that_lost_the_table_keeps_the_label(tmp_path: Path) -> None:
    db = _store(tmp_path)
    log_file = tmp_path / "state" / "access.log"
    token = jwt("jti-held", SIG_CHATGPT)
    with running_shim(tmp_path, log_file, db) as shim:
        _mcp_call(shim, token)
        assert _labels(shim, 1) == ["client=openai-mcp"]
    (tmp_path / "state" / "clients.json").unlink()
    with running_shim(tmp_path, log_file, db) as shim:
        _mcp_call(shim, token)
        assert _labels(shim, 2) == ["client=openai-mcp", "client=openai-mcp"]


def test_other_clients_exchanges_and_registrations_never_drop_a_valid_chatgpt_token(tmp_path: Path) -> None:
    token = jwt("jti-chatgpt", SIG_CHATGPT)
    with running_shim(tmp_path, oauth_db=_store(tmp_path)) as shim:
        _mcp_call(shim, token)
        flood = clients.MAX_TOKENS + clients.MAX_EXCHANGED + 1
        for n in range(flood):
            shim.upstream.routes["/.idp/register"] = (
                201,
                {"Content-Type": "application/json"},
                json.dumps({"client_id": f"other-{n}", "redirect_uris": [LOCAL_REDIRECT]}).encode(),
            )
            shim.request("POST", "/.idp/register", headers={"Content-Type": "application/json"}, body=b"{}")
            shim.upstream.routes["/.idp/token"] = (
                200,
                {"Content-Type": "application/json"},
                json.dumps({"access_token": jwt(f"jti-other-{n}")}).encode(),
            )
            shim.request(
                "POST",
                "/.idp/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=f"client_id=other-{n}".encode(),
            )
        _mcp_call(shim, token)
        assert _labels(shim, 2 + 2 * flood)[-1] == "client=openai-mcp"


@pytest.mark.parametrize(
    ("signature", "status"),
    [
        pytest.param(SIG_CHATGPT, 401, id="rejected-by-the-proxy-although-in-the-store"),
        pytest.param("f" * 342, 200, id="accepted-but-absent-from-the-store"),
        pytest.param(SIG_LOCAL, 200, id="issued-to-a-non-openai-client"),
    ],
)
def test_a_forged_rejected_or_foreign_token_stays_unverified(tmp_path: Path, signature: str, status: int) -> None:
    with running_shim(tmp_path, oauth_db=_store(tmp_path)) as shim:
        _mcp_call(shim, jwt("jti-x", signature), status=status)
        assert _labels(shim, 1) == ["client=unverified"]


def test_a_rejected_token_never_makes_the_shim_read_the_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lookups: list[str] = []

    def recording(path: Path, signature: str) -> oauthdb.IssuedClient | None:
        lookups.append(signature)
        return None

    monkeypatch.setattr(oauthdb, "client_of_token", recording)
    with running_shim(tmp_path, oauth_db=_store(tmp_path)) as shim:
        _mcp_call(shim, jwt("jti-x", SIG_CHATGPT), status=401)
        shim.log_lines(1)
    assert lookups == []
