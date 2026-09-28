"""Web-only adapter, for a printer whose only interface is its own web page.

Some printers publish a browser UI and no machine API at all, because the firmware
that serves the page exposes no state endpoint a client can rely on. Such a printer
is still worth an entry: the integration can hand the user the proxied page and a
liveness reading, and the card can say the host is up.

There is nothing else to read and nothing to send. This adapter answers one
question, whether the host answered, and leaves every other reading at its snapshot
default. The one capability such a printer is granted is ``web_ui``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Final

import aiohttp

from ..const import Command, PrintState, ProtocolId
from ..models import FileEntry, PrinterSnapshot
from ..protocols import Protocol, UnreachableError, UnsupportedCommandError

#: The same cap the Duet adapter applies, so a poll cannot outlive its interval.
REQUEST_TIMEOUT: Final[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=15)


class WebOnlyProtocol(Protocol):
    """A printer that offers a web page and nothing else.

    ``print_state`` is :attr:`PrintState.UNKNOWN` and never :attr:`PrintState.IDLE`.
    A web page cannot tell an idle machine from one that is printing, and ``IDLE`` is
    not a missing value here: the card renders it as a statement about the printer,
    so publishing it would be a fabrication the user reads as truth.
    """

    @property
    def web_url(self) -> str:
        """Return the page to probe, falling back to the printer's base URL."""
        return self._config.web_url or self._config.base_url

    async def _async_answered(self) -> bool:
        """Return whether the host answered the page request at all."""
        try:
            async with self._session.get(self.web_url, timeout=REQUEST_TIMEOUT):
                # Any status proves the host is alive, including 401 and 5xx: a page
                # that redirects or demands a password still answered. Only a
                # connection error or a timeout means unreachable.
                return True
        except (aiohttp.ClientError, TimeoutError):
            return False

    async def async_setup(self) -> None:
        """Prove the host answers, and raise when it does not. Idempotent."""
        if not await self._async_answered():
            raise UnreachableError(f"cannot reach {self.web_url}")

    async def async_teardown(self) -> None:
        """Return at once. This adapter holds nothing to close."""

    async def _async_read(self) -> PrinterSnapshot:
        """Return a snapshot that claims nothing beyond the host answering.

        An unreachable host is reported as ``connected=False`` rather than raised,
        because the contract reserves the raise for a reading this protocol cannot
        express, and every such reading already sits at its default.
        """
        return PrinterSnapshot(
            protocol=ProtocolId.WEB_ONLY,
            connected=await self._async_answered(),
            capabilities=self.capabilities,
            print_state=PrintState.UNKNOWN,
        )

    def async_subscribe(self) -> AsyncIterator[PrinterSnapshot] | None:
        """Return ``None``. A web page has no push channel to subscribe to."""
        return None

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        """Refuse every command.

        ``WEB_UI`` is the only capability this printer is granted, so the base class
        already refuses each command before this method is reached. It is implemented
        anyway so the refusal is explicit rather than inherited.
        """
        raise UnsupportedCommandError(command)

    async def async_list_files(self) -> Sequence[FileEntry]:
        """Return an empty list. A web page lists no file this adapter can name."""
        return []

    async def async_upload_file(
        self, name: str, stream: AsyncIterator[bytes], *, size: int | None = None
    ) -> FileEntry:
        """Refuse the upload. There is no file API behind a web page."""
        raise UnreachableError("this printer has no file API")
