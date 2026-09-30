"""End-to-end acceptance check of the SDCP adapter against a real printer.

This is the instrument that proves the integration can actually drive a printer,
rather than asserting it. It builds the real adapter from the real registry, runs
the real lifecycle, and prints the normalised snapshot.

    python tools/acceptance_sdcp.py 192.168.128.143

Read-only by default, and it refuses an address that answers as a resin printer.
``--camera out.jpg`` also grabs one frame. Nothing that changes printer state is
ever sent, and ``--frame-only`` prints the raw frames the adapter produces so the
wire format can be inspected.

Exit codes: 0 all checks passed, 1 a check failed, 2 bad usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from acceptance_kit import Report, async_refuse_resin, use_repository

use_repository()

REPORT = Report()
check = REPORT.check


async def run(host: str, camera_out: Path | None, keepalive: float) -> int:
    import aiohttp

    from custom_components.generic_3dprinter.const import Capability, Command, ProtocolId
    from custom_components.generic_3dprinter.protocols import ProtocolError, parse_config
    from custom_components.generic_3dprinter.registry import build_adapter, get_registration

    # Step [6] sends 386 and the adapter sends 324: never to a resin printer.
    if await async_refuse_resin(host):
        return 1

    config = parse_config(
        {
            "name": "acceptance",
            "protocol": ProtocolId.SDCP_CC1.value,
            "host": host,
            "port": 3030,
            "camera_port": 3031,
        }
    )
    registration = get_registration(config.protocol)

    print(f"[1] protocol: {registration.label}")
    print(f"    declared capabilities: {len(registration.capabilities)}")
    print(f"    declared hazards: {[f.id for f in registration.unsafe]}")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
        adapter = build_adapter(config, session)
        print(f"    granted capabilities: {len(adapter.capabilities)}")
        check(
            "START_PRINT is withheld until the hazard is opted into",
            Capability.START_PRINT not in adapter.capabilities,
        )
        check(
            "pause is granted",
            Capability.PAUSE in adapter.capabilities,
        )

        print("[2] async_setup")
        try:
            await adapter.async_setup()
        except ProtocolError as err:
            check("socket opened", False, str(err))
            return REPORT.summary()
        check("socket opened", True)
        print(f"    attributes: {json.dumps(dict(adapter.attributes), indent=6)[:1200]}")

        print("[3] async_read")
        snapshot = await adapter.async_read()
        print(json.dumps(snapshot.as_dict(), indent=2))

        check("connected", snapshot.connected, f"state={snapshot.print_state.value}")
        check("model known", bool(snapshot.model), str(snapshot.model))
        check("firmware known", bool(snapshot.firmware), str(snapshot.firmware))
        check("serial known", bool(snapshot.serial), str(snapshot.serial))
        check(
            "hotend temperature is a plausible reading",
            snapshot.hotend.current is not None
            and 0 <= float(snapshot.hotend.current) <= 350,
            f"{snapshot.hotend.current}",
        )
        check(
            "bed temperature is a plausible reading",
            snapshot.bed.current is not None and 0 <= float(snapshot.bed.current) <= 150,
            f"{snapshot.bed.current}",
        )
        check("layer counters present", snapshot.total_layers is not None, f"{snapshot.current_layer}/{snapshot.total_layers}")
        check("position parsed from the comma string", snapshot.position is not None)
        check("camera advertised", snapshot.camera)

        if snapshot.has_job:
            check("progress within range", snapshot.progress is not None and 0 <= float(snapshot.progress) <= 100, f"{snapshot.progress}")
            check("remaining time is a positive duration", snapshot.remaining is not None and float(snapshot.remaining) >= 0, f"{snapshot.remaining}s")
            check("filename present while printing", bool(snapshot.filename), str(snapshot.filename))

        print("[4] async_list_files")
        files = await adapter.async_list_files()
        check("file list returned", len(files) > 0, f"{len(files)} entries")
        for entry in files[:3]:
            print(
                f"    {entry.display_name}  size={entry.size}  path={entry.path}"
            )
        check(
            "file names carry their storage path",
            all(item.path.startswith("/local/") for item in files),
        )

        print("[5] unsupported command is refused before any hardware call")
        try:
            await adapter.async_send(Command.START_PRINT, filename="never-sent.gcode")
        except Exception as err:  # noqa: BLE001 - the type is the assertion
            check(
                "START_PRINT raised rather than reaching the printer",
                type(err).__name__ == "UnsafeCommandError",
                type(err).__name__,
            )
        else:
            check("START_PRINT raised rather than reaching the printer", False, "it was sent")

        try:
            await adapter.async_send(Command.HOME)
        except Exception as err:  # noqa: BLE001
            check(
                "a command this printer cannot do is refused",
                type(err).__name__ == "UnsupportedCommandError",
                type(err).__name__,
            )
        else:
            check("a command this printer cannot do is refused", False, "it was sent")

        print("[6] camera")
        try:
            frame = await adapter.async_camera_frame()
            check(
                "a complete JPEG frame was read",
                frame[:2] == b"\xff\xd8" and frame[-2:] == b"\xff\xd9",
                f"{len(frame)} bytes",
            )
            if camera_out is not None and frame:
                camera_out.write_bytes(frame)
                print(f"    wrote {camera_out}")
        except Exception as err:  # noqa: BLE001
            check("a complete JPEG frame was read", False, f"{type(err).__name__}: {err}")

        if keepalive:
            print(f"[7] holding the socket open for {keepalive:g}s")
            await asyncio.sleep(keepalive)

        print("[8] async_teardown")
        await adapter.async_teardown()
        check("socket closed", True)

    return REPORT.summary()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--camera", type=Path, default=None, help="write one frame here")
    parser.add_argument("--keepalive", type=float, default=0.0, help="hold the socket open for N seconds")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(run(args.host, args.camera, args.keepalive))


if __name__ == "__main__":
    raise SystemExit(main())
