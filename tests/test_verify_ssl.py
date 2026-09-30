"""The "Verify the TLS certificate" box: honoured by the entry's session, asked only where it means something."""

from __future__ import annotations

import asyncio
import datetime
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter.config_flow import Generic3DPrinterConfigFlow
from custom_components.generic_3dprinter.const import DOMAIN, ProtocolId
from custom_components.generic_3dprinter.registry import ADAPTERS
from custom_components.generic_3dprinter.runtime import create_session

#: The protocols that reach a printer over HTTP(S) at an address the user types.
ASKS = {ProtocolId.MOONRAKER, ProtocolId.OCTOPRINT, ProtocolId.DUET, ProtocolId.WEB_ONLY}


def _self_signed(directory: Path) -> ssl.SSLContext:
    """Return a server context with a certificate for 127.0.0.1 that nobody signed."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "printer.local")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("printer.local")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


@pytest.fixture(name="https_url")
async def https_url_fixture(tmp_path: Path) -> AsyncIterator[str]:
    """Serve one page over HTTPS with a self-signed certificate, as a printer's own UI does."""
    loop = asyncio.get_running_loop()
    handler = loop.get_exception_handler()

    def ignore_refused_handshake(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        # A client that rejects the certificate hangs up mid-handshake; that is the point.
        if not isinstance(context.get("exception"), ConnectionResetError):
            (handler or asyncio.AbstractEventLoop.default_exception_handler)(loop, context)

    loop.set_exception_handler(ignore_refused_handshake)
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="printer"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=_self_signed(tmp_path))
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"https://127.0.0.1:{port}/"
    finally:
        await runner.cleanup()
        loop.set_exception_handler(handler)


async def test_the_session_checks_the_certificate_unless_told_not_to(https_url: str) -> None:
    for session in (create_session(), create_session(True)):
        async with session:
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                await session.get(https_url)
    async with create_session(False) as session, session.get(https_url) as response:
        assert await response.text() == "printer"


@pytest.mark.parametrize(("verify", "state"), [(False, ConfigEntryState.LOADED), (True, ConfigEntryState.SETUP_RETRY)])
async def test_an_entry_honours_the_box(
    hass: HomeAssistant, https_url: str, verify: bool, state: ConfigEntryState
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Self-signed",
        data={
            "name": f"Self-signed {verify}",
            "protocol": "web_only",
            "host": "127.0.0.1",
            "web_url": https_url,
            "verify_ssl": verify,
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is state


@pytest.mark.parametrize("protocol", list(ADAPTERS))
def test_the_setup_form_asks_only_where_https_is_possible(protocol: ProtocolId) -> None:
    fields = {str(key) for key in Generic3DPrinterConfigFlow()._details_schema(ADAPTERS[protocol]).schema}
    assert ("verify_ssl" in fields) is (protocol in ASKS)


@pytest.mark.parametrize("protocol", list(ADAPTERS))
async def test_the_options_form_asks_only_where_https_is_possible(
    hass: HomeAssistant, protocol: ProtocolId
) -> None:
    # An entry made before the box was hidden keeps its key and still opens.
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Printer",
        data={"name": "Printer", "protocol": protocol.value, "host": "192.0.2.1", "verify_ssl": False},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = {str(key) for key in result["data_schema"].schema}
    assert ("verify_ssl" in fields) is (protocol in ASKS)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Printer", "host": "192.0.2.1"}
    )
    assert result["type"] == "create_entry"
    assert result["data"]["verify_ssl"] is False
