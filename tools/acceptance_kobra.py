"""Acceptance check of the Anycubic Kobra adapter against a real printer.

    python tools/acceptance_kobra.py 192.168.1.50
    python tools/acceptance_kobra.py 192.168.1.50 --camera out.flv
    python tools/acceptance_kobra.py 192.168.1.50 --active

Turn LAN Mode on at the printer first (Settings, Network, LAN Mode), and close
Anycubic's slicer and app, which share the printer's broker.

Read-only by default. It asks /info, runs the signed handshake, connects over TLS,
asks for every report, the multi-colour unit and the file list, and prints the raw
reports next to the snapshot the adapter builds from them, so a field read wrongly
shows up beside what the printer sent.

``--camera FILE`` starts the capture as Anycubic's client does, saves five seconds
of the stream, and opens a second reader at the same time to see whether the camera
serves two.

``--active`` also sends, each one shown and confirmed first: the light off and on,
the nozzle to 50 degrees and back to 0, the part fan to 30 % and back to 0, and on a
Kobra X a home of X and Y and a 10 mm jog of X there and back. It never starts,
pauses or stops a print. On models that apply settings only during a print, those
steps are expected to be refused while idle, and are reported as such.

Paste the whole output into an issue or a message: it contains no password.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance_kit import Report, add_active_arguments, confirm, use_repository  # noqa: E402

use_repository()


def _show(label: str, value: object, limit: int = 3000) -> None:
    text = json.dumps(value, indent=2, default=str)
    print(f"    {label}: {text[:limit]}{' ...' if len(text) > limit else ''}")


async def _read_stream(url: str, seconds: float) -> tuple[int, bytes]:
    import aiohttp

    received = bytearray()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=10)) as response:
                if response.status >= 400:
                    return response.status, b""
                deadline = time.monotonic() + seconds
                async for chunk in response.content.iter_chunked(65536):
                    received.extend(chunk)
                    if time.monotonic() > deadline:
                        break
                return response.status, bytes(received)
    except (aiohttp.ClientError, TimeoutError) as err:
        print(f"    stream error: {err!r}")
        return 0, bytes(received)


async def run(args: argparse.Namespace) -> int:
    import aiohttp

    from custom_components.generic_3dprinter.adapters import anycubic_kobra as kobra
    from custom_components.generic_3dprinter.const import Command
    from custom_components.generic_3dprinter.protocols import ProtocolError, parse_config
    from custom_components.generic_3dprinter.registry import build_adapter

    report = Report()
    async with aiohttp.ClientSession() as session:
        report.section(f"/info on {args.host}:{kobra.INFO_PORT}")
        try:
            info = await kobra.fetch_info(session, args.host, kobra.INFO_PORT)
        except ProtocolError as err:
            report.check("the printer answered /info", False, str(err))
            return report.summary()
        report.check("the printer answered /info", True)
        _show("info", {key: ("<token>" if key == "token" else value) for key, value in info.items()})
        report.check("LAN Mode is on", info.get("ctrlType") != "cloud", str(info.get("ctrlType")))
        try:
            kobra.check_info(info)
            report.check("the printer uses the signed handshake", True, f"model {info.get('modelId')}")
        except ProtocolError as err:
            report.check("the printer uses the signed handshake", False, str(err))
            return report.summary()

        config = parse_config(
            {
                "name": "acceptance",
                "protocol": "anycubic_kobra",
                "host": args.host,
                "serial": str(info.get("cn") or "") or None,
            }
        )
        adapter = build_adapter(config, session)
        try:
            report.section("handshake, TLS and reports")
            try:
                await adapter.async_setup()
            except ProtocolError as err:
                report.check("connected to the printer's broker", False, str(err))
                return report.summary()
            broker = adapter._broker_session  # noqa: SLF001 - the point is to show it
            client = adapter._client  # noqa: SLF001
            report.check("connected to the printer's broker", True, f"{broker.broker_host}:{broker.broker_port}")
            print(f"    client certificate supplied: {'yes' if broker.client_cert else 'no'}")
            print(f"    broker certificate sha256: {client.peer_certificate_sha256}")
            profile = adapter.model_profile
            report.check("the model has a profile", profile is not None, profile.name if profile else f"model {adapter.model_id}")

            print(f"    listening {args.listen:g}s")
            await asyncio.sleep(args.listen)
            snapshot = await adapter.async_read()
            _show("raw state", adapter._state)  # noqa: SLF001
            _show("raw lights", adapter._lights)  # noqa: SLF001
            _show("raw units", adapter._boxes)  # noqa: SLF001
            _show("raw peripherals", adapter._peripherie)  # noqa: SLF001
            _show("snapshot", snapshot.as_dict(), limit=6000)
            report.check("a print state was read", snapshot.print_state.value != "unknown", snapshot.print_state.value)
            report.check("the nozzle temperature was read", snapshot.hotend.current is not None)
            report.check("the bed temperature was read", snapshot.bed.current is not None)
            report.check("the firmware version was read", snapshot.firmware is not None, snapshot.firmware or "")
            report.check(
                "the multi-colour unit was read",
                snapshot.filament is not None,
                f"{len(snapshot.filament.slots)} slots" if snapshot.filament else "no unit reported",
            )

            report.section("file list")
            try:
                files = await adapter.async_list_files()
                report.check("the file list was read", True, f"{len(files)} files")
                for item in files[:10]:
                    print(f"    {item.name} ({item.size} bytes)")
            except ProtocolError as err:
                report.check("the file list was read", False, str(err))

            if args.camera:
                report.section("camera")
                try:
                    url = await adapter.async_stream_source()
                except ProtocolError as err:
                    report.check("the camera started", False, str(err))
                else:
                    report.check("the camera started", True, url)
                    first, second = await asyncio.gather(_read_stream(url, 5), _read_stream(url, 5))
                    report.check("the stream is FLV", first[1][:3] == b"FLV", f"HTTP {first[0]}, {len(first[1])} bytes")
                    Path(args.camera).write_bytes(first[1])
                    print(f"    saved to {args.camera}")
                    report.check("a second reader is served at the same time", len(second[1]) > 0, f"HTTP {second[0]}, {len(second[1])} bytes")

            report.section("commands")

            async def attempt(label: str, command: Command, **params: object) -> None:
                if not confirm(label, active=args.active, assume_yes=args.yes):
                    return
                reason = (adapter.last_snapshot.blocked if adapter.last_snapshot else {}).get(command)
                try:
                    await adapter.async_send(command, **params)
                except ProtocolError as err:
                    report.check(label, reason is not None, f"refused: {err}")
                    return
                await asyncio.sleep(2)
                after = await adapter.async_read()
                report.check(label, True, f"state {after.print_state.value}, nozzle target {after.hotend.target}, fan {after.fans.model}, lights {sorted(item.value for item in after.lights)}")

            async def settle(seconds: float = 30) -> None:
                # A Kobra X reports busy while it moves, and refuses the next move until idle.
                deadline = time.monotonic() + seconds
                while (await adapter.async_read()).print_state.value != "idle" and time.monotonic() < deadline:
                    await asyncio.sleep(1)

            await attempt("light off", Command.SET_LIGHT, on=False)
            await attempt("light on", Command.SET_LIGHT, on=True)
            await attempt("nozzle target 50", Command.SET_HOTEND_TEMP, value=50)
            await attempt("nozzle target 0", Command.SET_HOTEND_TEMP, value=0)
            await attempt("part fan 30 %", Command.SET_FAN_SPEED, value=30)
            await attempt("part fan 0 %", Command.SET_FAN_SPEED, value=0)
            if adapter.kobra_x:
                await attempt("home X and Y", Command.HOME, axes="XY")
                await settle()
                await attempt("jog X +10 mm", Command.JOG, axis="X", distance=10)
                await settle()
                await attempt("jog X -10 mm", Command.JOG, axis="X", distance=-10)
        finally:
            await adapter.async_teardown()
    return report.summary()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("host", help="the printer's address")
    parser.add_argument("--camera", metavar="FILE", help="start the camera and save 5 s of it to FILE")
    parser.add_argument("--listen", type=float, default=5.0, help="seconds to collect reports before reading")
    add_active_arguments(parser)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
