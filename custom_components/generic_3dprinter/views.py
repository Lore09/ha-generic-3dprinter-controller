"""HTTP endpoints that reach a printer through Home Assistant.

Two things are proxied here, and both exist for the same reason: the browser is
often unable to reach the printer itself.

* **The camera.** Frames are read upstream by the integration and re-served from
  the Home Assistant origin, so a dashboard on ``https://`` can show a printer that
  only speaks plain HTTP, with no mixed content and no printer credentials in the
  page.
* **The printer's own web UI.** For a printer that exposes no control API, its
  embedded page is reverse-proxied so it can be embedded in a dashboard at all.

Every request is authorised by an HMAC-signed token carried in a **path segment**.
A browser cannot attach an ``Authorization`` header to an ``<img>``, ``<script>`` or
``<link>`` sub-resource, and the upstream target lives inside the signed payload, so
the proxy cannot be pointed at an arbitrary host and is not an open proxy.

The web proxy carries the upstream **port** as a path segment, because a printer's
page and its control socket are routinely on different ports while the proxy has
one origin::

    /api/generic_3dprinter/{entry_id}/{token}/p{port}/{path}
"""

from __future__ import annotations

import logging
from contextlib import aclosing, suppress
from http import HTTPStatus
from typing import Final
from urllib.parse import quote, unquote

from aiohttp import hdrs, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import CONTENT_TYPE_MULTIPART
from homeassistant.core import HomeAssistant

from .const import (
    API_BASE,
    DATA_COORDINATORS,
    RESOURCE_CAMERA,
    RESOURCE_SNAPSHOT,
    RESOURCE_STATUS,
    RESOURCE_WEB,
    Capability,
)
from .coordinator import PrinterError
from .protocols import UPLOAD_SUFFIXES
from .proxy import WebProxyError, scrub_response_headers
from .runtime import get_runtime
from .security import InvalidToken, MediaToken

_LOGGER = logging.getLogger(__name__)

#: Boundary used for the MJPEG streams this integration produces.
STREAM_BOUNDARY: Final = "frameboundary"

#: Chunk size used when piping a body to the browser.
STREAM_CHUNK: Final = 65536

#: Headers copied verbatim from an unrewritten upstream response.
_PASSTHROUGH_HEADERS: Final = (
    hdrs.CONTENT_TYPE,
    hdrs.CONTENT_LENGTH,
    hdrs.CONTENT_RANGE,
    hdrs.ACCEPT_RANGES,
    hdrs.ETAG,
    hdrs.LAST_MODIFIED,
    hdrs.CACHE_CONTROL,
)


class ProxyView(HomeAssistantView):
    """Base class mapping errors and tokens for every proxy view."""

    #: Browser elements cannot send an Authorization header, so these views
    #: authenticate with the signed token in the URL path instead. Declaring
    #: ``requires_auth = False`` is what lets the request reach the handler at all.
    requires_auth = False

    #: Token scopes this view accepts.
    resources: frozenset[str] = frozenset()

    def __init__(self, hass: HomeAssistant) -> None:
        """Store the Home Assistant instance."""
        self.hass = hass

    async def get(self, request: web.Request, **kwargs: str) -> web.StreamResponse:
        """Authorise the request and delegate to the subclass handler."""
        try:
            return await self.handle(request, **kwargs)
        except InvalidToken as err:
            _LOGGER.debug("%s: rejected request: %s", self.name, err)
            raise _rejected(request) from err
        except WebProxyError as err:
            _LOGGER.debug("%s: %s", self.name, err)
            return web.json_response({"error": str(err)}, status=HTTPStatus.BAD_GATEWAY)

    async def handle(self, request: web.Request, **kwargs: str) -> web.StreamResponse:
        """Serve the request; implemented by subclasses."""
        raise NotImplementedError

    def _runtime(self, entry_id: str):
        runtime = get_runtime(self.hass, entry_id)
        if runtime is None:
            raise web.HTTPNotFound
        return runtime

    def _token(self, runtime, entry_id: str, token: str) -> MediaToken:
        media = runtime.tokens.async_verify(token, entry_id=entry_id)
        if media.resource not in self.resources:
            raise InvalidToken(
                f"token for {media.resource!r} cannot be used on {self.name}"
            )
        return media


def _rejected(request: web.Request) -> web.HTTPException:
    """Return the right rejection status for a proxied request.

    Home Assistant answers 401 only when credentials were actually offered, so a
    stale image URL is not counted as a failed login by the ban middleware.
    """
    if hdrs.AUTHORIZATION in request.headers:
        return web.HTTPUnauthorized
    return web.HTTPForbidden


async def async_write_eof(response: web.StreamResponse) -> None:
    """Close a stream response, ignoring an already gone peer."""
    with suppress(ConnectionResetError, RuntimeError):
        await response.write_eof()


async def async_proxy_mjpeg_stream(
    request: web.Request, runtime, *, camera_id: int | None = None
) -> web.StreamResponse:
    """Serve the shared camera stream of ``runtime`` as multipart JPEG."""
    response = web.StreamResponse(status=HTTPStatus.OK)
    response.content_type = CONTENT_TYPE_MULTIPART.format(STREAM_BOUNDARY)
    response.headers[hdrs.CACHE_CONTROL] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers[hdrs.PRAGMA] = "no-cache"
    response.headers[hdrs.EXPIRES] = "0"
    await response.prepare(request)

    boundary = STREAM_BOUNDARY.encode("ascii")
    try:
        first = True
        # aclosing, not ``async with``: the hub hands back an async generator, and a
        # generator is not a context manager. Getting this wrong closes the response
        # on the first line and the viewer sees an empty stream with no error.
        async with aclosing(runtime.camera.async_subscribe()) as frames:
            async for frame in frames:
                if not frame:
                    continue
                part = (
                    b"--"
                    + boundary
                    + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode("ascii")
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
                await response.write(part)
                if first:
                    # Chrome renders frame n-1, so the first frame is sent twice.
                    await response.write(part)
                    first = False
    except ConnectionResetError:
        _LOGGER.debug("%s: viewer disconnected from the camera stream", runtime.config.name)
    except Exception as err:  # noqa: BLE001 - a broken stream must not take the view down
        _LOGGER.warning("%s: the camera stream ended: %s", runtime.config.name, err)
    finally:
        await async_write_eof(response)
    return response


class CameraStreamView(ProxyView):
    """Multipart MJPEG stream of a printer's camera."""

    url = f"{API_BASE}/{{entry_id}}/camera.mjpeg/{{token}}"
    name = "api:generic_3dprinter:camera"
    resources = frozenset({RESOURCE_CAMERA})

    async def handle(self, request: web.Request, entry_id: str, token: str) -> web.StreamResponse:
        """Stream camera frames to the browser through one shared upstream connection."""
        runtime = self._runtime(entry_id)
        self._token(runtime, entry_id, token)
        if not runtime.has_camera:
            raise web.HTTPNotFound
        return await async_proxy_mjpeg_stream(request, runtime)


class SnapshotView(ProxyView):
    """A single still frame, for a card tile, a notification or an automation."""

    url = f"{API_BASE}/{{entry_id}}/snapshot.jpg/{{token}}"
    name = "api:generic_3dprinter:snapshot"
    resources = frozenset({RESOURCE_SNAPSHOT})

    async def handle(self, request: web.Request, entry_id: str, token: str) -> web.StreamResponse:
        """Return one JPEG frame."""
        runtime = self._runtime(entry_id)
        self._token(runtime, entry_id, token)
        frame = await runtime.camera.async_refresh_frame()
        if not frame:
            raise web.HTTPNotFound
        return web.Response(
            body=frame,
            content_type="image/jpeg",
            headers={hdrs.CACHE_CONTROL: "no-store"},
        )


class StatusView(ProxyView):
    """A JSON health and capability report, used by the card and for debugging."""

    url = f"{API_BASE}/{{entry_id}}/status/{{token}}"
    name = "api:generic_3dprinter:status"
    resources = frozenset({RESOURCE_STATUS})

    async def handle(self, request: web.Request, entry_id: str, token: str) -> web.StreamResponse:
        """Return the current status document."""
        runtime = self._runtime(entry_id)
        self._token(runtime, entry_id, token)
        return self.json(runtime.describe())


class WebProxyView(ProxyView):
    """Reverse proxy for a printer's own web UI, with the port as a path segment.

    ``{path:.*}`` is greedy across slashes, so a deep asset path routes here. The
    route shape must stay distinct from every sibling under the same base; a
    routing test covers that rather than a comment.
    """

    url = f"{API_BASE}/{{entry_id}}/web/{{token}}/p{{port}}/{{path:.*}}"
    name = "api:generic_3dprinter:web"
    resources = frozenset({RESOURCE_WEB})

    async def handle(
        self, request: web.Request, entry_id: str, token: str, port: str, path: str
    ) -> web.StreamResponse:
        """Proxy one request to the printer, rewriting a document when it is safe."""
        runtime = self._runtime(entry_id)
        self._token(runtime, entry_id, token)

        if not runtime.has_web_ui:
            raise web.HTTPNotFound

        try:
            upstream_port = int(port)
        except (TypeError, ValueError) as err:
            raise web.HTTPNotFound from err

        if not runtime.web_proxy.allow_port(upstream_port):
            raise web.HTTPNotFound

        return await runtime.web_proxy.async_proxy(
            request,
            runtime.session,
            port=upstream_port,
            path=path,
            entry_id=entry_id,
            token=token,
        )


#: The largest file the upload endpoint accepts. Every adapter holds the whole
#: file in memory to compute its checksum, so a ceiling is a memory bound as much
#: as a sanity bound. A sliced model is rarely a tenth of this.
MAX_UPLOAD_BYTES: Final = 256 * 1024 * 1024


def upload_name(raw: str | None, suffixes: tuple[str, ...] = UPLOAD_SUFFIXES) -> str:
    """Return a safe file name for an upload, or raise ``HTTPBadRequest``.

    The name travels to the printer as a header or a form field, so only its last
    path segment is kept and control characters are refused outright. A client may
    percent-encode the name in its multipart header, and the separators hidden that
    way are removed too.
    """
    name = unquote(raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in (".", "..") or any(ord(char) < 32 for char in name):
        raise web.HTTPBadRequest(text="the file needs a name")
    if not name.lower().endswith(suffixes):
        raise web.HTTPBadRequest(
            text=f"only {', '.join(suffixes)} files can be sent to this printer"
        )
    if len(name) > 200:
        raise web.HTTPBadRequest(text="the file name is too long")
    return name


class UploadView(HomeAssistantView):
    """Receive a print file from the card and store it on the printer.

    Unlike the proxy views this one is called with ``fetch``, which carries the
    user's own credentials, so Home Assistant authenticates it as it does any API
    call. The body is multipart with one ``file`` field and is read in chunks, so
    Home Assistant's request size limit, meant for JSON bodies, does not apply.
    """

    url = f"{API_BASE}/{{entry_id}}/upload"
    name = "api:generic_3dprinter:upload"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Store the Home Assistant instance."""
        self.hass = hass

    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        """Store the uploaded file and return its entry."""
        runtime = get_runtime(self.hass, entry_id)
        coordinator = self.hass.data.get(DATA_COORDINATORS, {}).get(entry_id)
        if runtime is None or coordinator is None:
            raise web.HTTPNotFound
        if Capability.FILE_UPLOAD not in runtime.capabilities:
            return web.json_response(
                {"error": "this printer does not accept uploads"}, status=HTTPStatus.BAD_REQUEST
            )

        try:
            reader = await request.multipart()
        except (AssertionError, ValueError) as err:
            raise web.HTTPBadRequest(text="expected a multipart upload") from err
        part = await reader.next()
        while part is not None and getattr(part, "name", None) != "file":
            part = await reader.next()
        if part is None or not hasattr(part, "read_chunk"):
            raise web.HTTPBadRequest(text="the upload has no file field")

        name = upload_name(part.filename, runtime.upload_suffixes)
        body = bytearray()
        while chunk := await part.read_chunk(STREAM_CHUNK):
            body.extend(chunk)
            if len(body) > MAX_UPLOAD_BYTES:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=MAX_UPLOAD_BYTES, actual_size=len(body)
                )
        if not body:
            raise web.HTTPBadRequest(text="the file is empty")

        try:
            entry = await coordinator.async_upload_file(name, bytes(body))
        except PrinterError as err:
            return web.json_response({"error": str(err)}, status=HTTPStatus.BAD_GATEWAY)
        return web.json_response({"file": entry.as_dict()})


VIEWS: Final = (
    CameraStreamView,
    SnapshotView,
    StatusView,
    WebProxyView,
    UploadView,
)


def async_register_views(hass: HomeAssistant) -> None:
    """Register every proxy view once per Home Assistant instance."""
    for view in VIEWS:
        hass.http.register_view(view(hass))


def proxy_url(entry_id: str, token: str, port: int, path: str = "") -> str:
    """Return the proxied URL for one upstream path, safely quoted."""
    cleaned = path.lstrip("/")
    encoded = quote(cleaned, safe="/~@:+,$;=!*'()[]")
    return f"{API_BASE}/{entry_id}/{RESOURCE_WEB}/{token}/p{port}/{encoded}"


def camera_url(entry_id: str, token: str) -> str:
    """Return the proxied camera stream URL."""
    return f"{API_BASE}/{entry_id}/{RESOURCE_CAMERA}/{token}"


def snapshot_url(entry_id: str, token: str) -> str:
    """Return the proxied snapshot URL."""
    return f"{API_BASE}/{entry_id}/{RESOURCE_SNAPSHOT}/{token}"


def status_url(entry_id: str, token: str) -> str:
    """Return the proxied status URL."""
    return f"{API_BASE}/{entry_id}/{RESOURCE_STATUS}/{token}"
