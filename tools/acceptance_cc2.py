"""End-to-end acceptance check of the Centauri Carbon 2 adapter against a real printer.

It builds the real adapter from the real registry, runs the real lifecycle, and
prints the normalised snapshot next to the raw status it came from, so a field the
adapter reads wrongly shows up side by side with what the printer sent.

    python tools/acceptance_cc2.py 192.168.128.146
    python tools/acceptance_cc2.py 192.168.128.146 --access-code 123456 --camera out.jpg

Read-only. What it sends is the discovery request, the registration, the
heartbeat, and methods 1001 (attributes), 1002 (status) and 1044 (file list);
``--camera`` adds 1042, which turns the camera stream on. Nothing that moves or
heats the printer is ever sent.

The printer holds very few client slots, so close the slicer's device page first
if the registration reports that none is free.

Exit codes: 0 all checks passed, 1 a check failed, 2 bad usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from acceptance_kit import Report  # noqa: E402 - tools/ is the script's own directory

REPORT = Report()
check = REPORT.check


async def run(host: str, access_code: str | None, camera_out: Path | None, listen: float) -> int:
    import aiohttp

    from custom_components.generic_3dprinter.const import Capability, ProtocolId
    from custom_components.generic_3dprinter.adapters.elegoo_cc2 import async_discover_cc2
    from custom_components.generic_3dprinter.protocols import ProtocolError, parse_config
    from custom_components.generic_3dprinter.registry import build_adapter, get_registration

    print("[1] discovery on UDP 52700")
    found = await async_discover_cc2(host, timeout=3)
    if not check("the printer answered the discovery request", found is not None):
        return 1
    assert found is not None
    print(f"    {json.dumps(found.as_dict(), indent=6)}")
    if not check(
        "the printer is in LAN-only mode",
        found.lan_only is True,
        "" if found.lan_only else "turn it on under Settings, Network, LAN Only Mode",
    ):
        return 1
    check(
        "an access code was given if the printer has one set",
        not found.access_code_set or bool(access_code),
    )

    data = {
        "name": "acceptance",
        "protocol": ProtocolId.ELEGOO_CC2.value,
        "host": host,
        "serial": found.mainboard_id,
    }
    if access_code:
        data["access_code"] = access_code
    config = parse_config(data)
    registration = get_registration(config.protocol)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
        adapter = build_adapter(config, session)
        check(
            "START_PRINT is withheld until the hazard is opted into",
            Capability.START_PRINT not in adapter.capabilities,
        )
        print(f"    protocol: {registration.label}, client id {adapter.client_id}")

        try:
            print("[2] connect, register, attributes and full status")
            try:
                await adapter.async_setup()
            except ProtocolError as err:
                check("registered with the printer", False, str(err))
                return 1
            check("registered with the printer", True)
            check("attributes arrived", bool(adapter.attributes))
            print(f"    attributes: {json.dumps(dict(adapter.attributes), indent=6)[:2000]}")

            print(f"[3] listening {listen:g}s for status pushes")
            await asyncio.sleep(listen)
            raw = adapter._status  # noqa: SLF001 - the point is to show the raw status
            print(f"    raw status: {json.dumps(raw, indent=6)[:4000]}")

            print("[4] async_read")
            snapshot = await adapter.async_read()
            print(json.dumps(snapshot.as_dict(), indent=2))
            check("the snapshot says connected", snapshot.connected)
            check("a print state was read", snapshot.print_state.value != "unknown",
                  snapshot.print_state.value)
            check("the nozzle temperature was read", snapshot.hotend.current is not None)
            check("the bed temperature was read", snapshot.bed.current is not None)
            check("the firmware version was read", snapshot.firmware is not None,
                  snapshot.firmware or "")

            print("[5] file list")
            try:
                files = await adapter.async_list_files()
            except ProtocolError as err:
                check("the file list was read", False, str(err))
            else:
                check("the file list was read", True, f"{len(files)} files")
                for item in files[:10]:
                    print(f"    {item.name} ({item.size} bytes)")

            if camera_out is not None:
                print("[6] camera")
                try:
                    frame = await adapter.async_camera_frame()
                except ProtocolError as err:
                    check("a camera frame arrived", False, str(err))
                else:
                    camera_out.write_bytes(frame)
                    check("a camera frame arrived", frame.startswith(b"\xff\xd8"),
                          f"{len(frame)} bytes written to {camera_out}")
        finally:
            await adapter.async_teardown()

    return REPORT.summary()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("host")
    parser.add_argument("--access-code", default=None)
    parser.add_argument("--camera", type=Path, default=None, help="write one frame here")
    parser.add_argument("--listen", type=float, default=15.0, help="seconds to collect pushes")
    args = parser.parse_args()
    return asyncio.run(run(args.host, args.access_code, args.camera, args.listen))


if __name__ == "__main__":
    sys.exit(main())
