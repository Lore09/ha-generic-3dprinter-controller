"""Uploads to a fake Saturn: the chunked form on the socket's own port, only its own file
types, only while idle, and a stored file only when the printer says so."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from custom_components.generic_3dprinter.adapters import sdcp
from custom_components.generic_3dprinter.adapters.sdcp_resin import RESIN_SUFFIXES, SdcpResinProtocol
from custom_components.generic_3dprinter.const import Capability
from custom_components.generic_3dprinter.protocols import (
    UPLOAD_SUFFIXES,
    CommandRejectedError,
    parse_config,
)
from custom_components.generic_3dprinter.registry import build_adapter
from custom_components.generic_3dprinter.views import upload_name
from tests.fake_resin_printer import FakeResinPrinter, load_fixture

SATURN = load_fixture()["attributes"]["Attributes"]["MainboardID"]
FORM_FIELDS = {"Check", "S-File-MD5", "Offset", "Uuid", "TotalSize", "File", "filename"}


@pytest.fixture(name="session")
async def session_fixture() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture(name="printing")
async def printing_fixture() -> AsyncIterator[FakeResinPrinter]:
    server = FakeResinPrinter(printing=True)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _adapter(port: int, session: aiohttp.ClientSession) -> SdcpResinProtocol:
    config = parse_config(
        {"name": "Saturn", "protocol": "sdcp_resin", "host": "127.0.0.1", "port": port, "serial": SATURN}
    )
    adapter = build_adapter(config, session)
    assert isinstance(adapter, SdcpResinProtocol)
    return adapter


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _local(printer: FakeResinPrinter) -> list[str]:
    return [item["name"] for item in printer.files if item["name"].startswith("/local/")]


# ----------------------------------------------------------------- the wire


async def test_a_goo_file_is_posted_to_the_socket_port(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    adapter = _adapter(resin_printer.port, session)
    assert adapter.upload_url == f"http://127.0.0.1:{resin_printer.port}/uploadFile/upload"
    body = b"GOO sliced layers"
    entry = await adapter.async_upload_file("folder/part.goo", _chunks(body), size=len(body))

    assert (entry.name, entry.path, entry.size) == ("part.goo", "/local/part.goo", len(body))
    (chunk,) = resin_printer.uploads
    assert set(chunk) == FORM_FIELDS
    assert chunk["File"] == body
    assert chunk["filename"] == "part.goo"
    assert chunk["S-File-MD5"] == hashlib.md5(body).hexdigest()  # noqa: S324
    assert (chunk["Check"], chunk["Offset"], chunk["TotalSize"]) == ("1", "0", str(len(body)))
    assert "/local/part.goo" in _local(resin_printer)
    assert "/local/part.goo" in [item.path for item in await adapter.async_list_files()]
    # The socket carried only reads: no command stores or starts anything.
    assert set(resin_printer.sent_commands) <= {0, 1, 258}
    assert resin_printer.forbidden == []
    await adapter.async_teardown()


async def test_a_large_file_goes_in_chunks_of_one_transfer(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdcp, "UPLOAD_CHUNK", 4)
    adapter = _adapter(resin_printer.port, session)
    body = b"0123456789"
    await adapter.async_upload_file("part.ctb", _chunks(body))
    uploads = resin_printer.uploads
    assert [item["Offset"] for item in uploads] == ["0", "4", "8"]
    assert b"".join(item["File"] for item in uploads) == body
    assert len({item["Uuid"] for item in uploads}) == 1
    assert {item["S-File-MD5"] for item in uploads} == {hashlib.md5(body).hexdigest()}  # noqa: S324
    assert "/local/part.ctb" in _local(resin_printer)
    await adapter.async_teardown()


# ----------------------------------------------------------------- refusals


@pytest.mark.parametrize("name", ["part.gcode", "part.bgcode", "part.stl", "part"])
async def test_a_file_the_printer_does_not_print_never_leaves(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, name: str
) -> None:
    adapter = _adapter(resin_printer.port, session)
    with pytest.raises(CommandRejectedError, match=r"\.ctb, \.goo"):
        await adapter.async_upload_file(name, _chunks(b"G28\n"))
    assert resin_printer.uploads == []
    assert resin_printer.sent_commands == []
    await adapter.async_teardown()


async def test_a_printing_machine_takes_no_file(
    printing: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    adapter = _adapter(printing.port, session)
    with pytest.raises(CommandRejectedError, match="idle") as caught:
        await adapter.async_upload_file("part.goo", _chunks(b"x"))
    assert caught.value.reason == "the printer is not idle"
    assert printing.uploads == []
    assert set(printing.sent_commands) <= {0, 1}
    await adapter.async_teardown()


@pytest.mark.parametrize("current", [[2], [3], [4], [8]])
async def test_a_busy_machine_takes_no_file(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, current: list[int]
) -> None:
    resin_printer.status["CurrentStatus"] = current
    adapter = _adapter(resin_printer.port, session)
    with pytest.raises(CommandRejectedError, match="idle"):
        await adapter.async_upload_file("part.goo", _chunks(b"x"))
    assert resin_printer.uploads == []
    await adapter.async_teardown()


async def test_a_refused_chunk_stops_the_upload(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdcp, "UPLOAD_CHUNK", 4)
    resin_printer.upload_code = "100002"
    adapter = _adapter(resin_printer.port, session)
    with pytest.raises(CommandRejectedError, match="refused the upload") as caught:
        await adapter.async_upload_file("part.goo", _chunks(b"0123456789"))
    assert caught.value.code == "100002"
    assert len(resin_printer.uploads) == 1
    assert "/local/part.goo" not in _local(resin_printer)
    await adapter.async_teardown()


async def test_a_page_that_is_not_the_printers_reply_is_no_upload(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    """A reply without the spec's code 000000 fails, where the Centauri's upload lets it pass."""
    resin_printer.upload_code = None
    adapter = _adapter(resin_printer.port, session)
    with pytest.raises(CommandRejectedError, match="HTTP 200") as caught:
        await adapter.async_upload_file("part.goo", _chunks(b"x"))
    assert caught.value.code == 200
    await adapter.async_teardown()


# ----------------------------------------------------------------- the file types


@pytest.mark.parametrize(
    ("named", "suffixes"),
    [
        (["CTB", "GOO"], (".ctb", ".goo")),
        (["GOO"], (".goo",)),
        ("CTB", (".ctb",)),
        ([" prz ", "cbddlp"], (".prz", ".cbddlp")),
        (["CTB", "gcode", "stl", "CTB"], (".ctb",)),
        (["gcode"], RESIN_SUFFIXES),
        ([], RESIN_SUFFIXES),
        (None, RESIN_SUFFIXES),
        ({"CTB": 1}, RESIN_SUFFIXES),
    ],
)
def test_the_suffixes_are_the_resin_types_the_printer_names(named: Any, suffixes: tuple[str, ...]) -> None:
    adapter = _adapter(3030, None)  # type: ignore[arg-type]
    if named is not None:
        adapter._attributes["SupportFileType"] = named  # noqa: SLF001
    assert adapter.upload_suffixes == suffixes


async def test_the_captured_saturn_takes_ctb_and_goo(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    adapter = _adapter(resin_printer.port, session)
    snapshot = await adapter.async_read()
    assert Capability.FILE_UPLOAD in snapshot.capabilities
    assert adapter.upload_suffixes == (".ctb", ".goo")
    await adapter.async_teardown()


def test_the_centauri_keeps_its_g_code_types() -> None:
    config = parse_config({"name": "CC1", "protocol": "sdcp_cc1", "host": "127.0.0.1"})
    adapter = build_adapter(config, None)  # type: ignore[arg-type]
    assert adapter.upload_suffixes == UPLOAD_SUFFIXES == (".gcode", ".gco", ".g", ".bgcode")
    assert adapter.upload_url == "http://127.0.0.1/uploadFile/upload"
    assert adapter.upload_reply_required is False  # type: ignore[attr-defined]


def test_the_upload_view_checks_the_printers_own_types() -> None:
    assert upload_name("part.goo", (".ctb", ".goo")) == "part.goo"
    assert upload_name("C:\\slices\\Part.CTB", (".ctb", ".goo")) == "Part.CTB"
    assert upload_name("part.gcode") == "part.gcode"
    with pytest.raises(web.HTTPBadRequest) as caught:
        upload_name("part.gcode", (".ctb", ".goo"))
    assert caught.value.text == "only .ctb, .goo files can be sent to this printer"
    with pytest.raises(web.HTTPBadRequest):
        upload_name("part.goo")
