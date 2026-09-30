"""Check the Elegoo resin adapter on a real printer, read-only: it sends commands 0, 1, 258,
320 and 321 only, and fails if any other went out. ``python tools/acceptance_sdcp_resin.py HOST``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance_kit import Report, use_repository  # noqa: E402

use_repository()

#: The command codes this tool may put on the wire; every one of them only reads.
READS = frozenset({0, 1, 258, 320, 321})
#: The ports worth knowing: a web page, RTSP, SDCP, and the Centauri's MJPEG camera.
PORTS = (80, 443, 554, 3030, 3031, 8080)
#: How many history entries are asked for, one at a time, to find the last job's.
HISTORY_LOOKUPS = 5


def _show(label: str, value: object, limit: int = 3000) -> None:
    text = json.dumps(value, indent=2, default=str)
    print(f"    {label}: {text[:limit]}{' ...' if len(text) > limit else ''}")


def _record_commands(adapter: Any) -> list[int]:
    """Return a list that gets the command code of every frame the adapter builds.
    The heartbeat builds its frames the same way, so they are counted too."""
    sent: list[int] = []
    build = adapter._frame  # noqa: SLF001 - the point is to watch the wire

    def frame(cmd: int, data: Any) -> tuple[str, str]:
        sent.append(cmd)
        return build(cmd, data)

    adapter._frame = frame  # noqa: SLF001
    return sent


def _record_topics(adapter: Any) -> Counter[str]:
    """Return a count of every frame the printer sends, by the kind its topic names."""
    kinds: Counter[str] = Counter()
    handle = adapter._handle_frame  # noqa: SLF001

    def handle_frame(raw: str) -> None:
        try:
            topic = str(json.loads(raw).get("Topic") or "")
        except (ValueError, AttributeError):
            topic = ""
        kinds[topic.split("/")[1] if topic.count("/") else "(none)"] += 1
        handle(raw)

    adapter._handle_frame = handle_frame  # noqa: SLF001
    return kinds


def check_snapshot(report: Report, snapshot: Any) -> None:
    """Check what one snapshot says and its units: a resin printer has no nozzle
    or bed, and shows progress, times and layers only for a job in hand."""
    from custom_components.generic_3dprinter.const import Capability, PrintState

    resin = snapshot.resin
    check = report.check
    check("connected", snapshot.connected, f"state {snapshot.print_state.value}")
    check("model known", bool(snapshot.model), str(snapshot.model))
    check("firmware known", bool(snapshot.firmware), str(snapshot.firmware))
    check("serial known", bool(snapshot.serial), str(snapshot.serial))
    check("a print state was read", snapshot.print_state is not PrintState.UNKNOWN, snapshot.print_state.value)
    check("the resin state was read", resin is not None and resin.phase is not None, str(resin and resin.phase))
    check(
        "no nozzle or bed is reported",
        snapshot.hotend.current is None and snapshot.bed.current is None,
        f"nozzle {snapshot.hotend.current}, bed {snapshot.bed.current}",
    )
    if resin is None:
        return
    uv = resin.uv_led
    check("the UV LED temperature is plausible", uv is not None and 0 <= float(uv) <= 100, f"{uv} °C")
    if Capability.VAT_SENSOR in snapshot.capabilities:
        vat = resin.vat
        check(
            "the vat temperature is plausible",
            vat.current is not None and 0 <= float(vat.current) <= 80,
            f"{vat.current} -> {vat.target} °C, heat status {resin.vat_heat_status}",
        )
    film, rated = resin.release_film, resin.release_film_max
    check("the release film count is read", film is not None and film >= 0 and bool(rated), f"{film} of {rated}")
    streams = f"{resin.video_streams} of {resin.video_streams_max}"
    check("the camera streams are counted", resin.video_streams is not None, streams)
    if resin.device_faults:
        print(f"    device checks failing: {', '.join(resin.device_faults)}")

    job = (snapshot.progress, snapshot.elapsed, snapshot.remaining, snapshot.current_layer, snapshot.total_layers)
    shown = f"progress {job[0]}, elapsed {job[1]} s, remaining {job[2]} s, layer {job[3]}/{job[4]}"
    if snapshot.print_state in (PrintState.PREPARING, PrintState.PRINTING, PrintState.PAUSED):
        progress, elapsed, remaining, layer, layers = job
        check("progress within range", progress is not None and 0 <= float(progress) <= 100, shown)
        check(
            "the times are seconds of a plausible job",
            elapsed is not None and remaining is not None and 0 <= float(elapsed) + float(remaining) < 7 * 86400,
            shown,
        )
        check("the layers are counted", layer is not None and bool(layers) and layer <= layers, shown)
    elif snapshot.print_state is PrintState.IDLE:
        check("an idle printer shows no job", all(item is None for item in job), shown)
        print(f"    last file kept: {snapshot.filename}")


async def check_history(report: Report, adapter: Any, saved: dict[str, Any]) -> None:
    """Read the history (320) and the entries (321) until the last job's is found,
    and check its begin and end against its ticks, which should be milliseconds."""
    history = await adapter._async_request(320)  # noqa: SLF001
    saved["history"] = history
    ids = (history or {}).get("HistoryData")
    if not report.check("the history was read", isinstance(ids, list), f"{len(ids or [])} entries"):
        return
    info = adapter._status.get("PrintInfo") or {}  # noqa: SLF001
    filename, ticks = str(info.get("Filename") or ""), info.get("TotalTicks")
    saved["history_detail"] = []
    for entry in ids[:HISTORY_LOOKUPS]:
        detail = await adapter._async_request(321, {"Id": [entry]})  # noqa: SLF001
        saved["history_detail"].append(detail)
        for item in (detail or {}).get("HistoryDetailList") or []:
            _show("entry", {key: value for key, value in item.items() if key != "SliceInformation"})
            if not filename or not str(item.get("TaskName") or "").endswith(filename):
                continue
            took = float(item.get("EndTime") or 0) - float(item.get("BeginTime") or 0)
            seconds = float(ticks or 0) / 1000
            report.check(
                "the last job's ticks are milliseconds",
                abs(took - seconds) <= max(5.0, seconds * 0.02),
                f"it took {took:g} s, its ticks say {seconds:g} s",
            )
            return
    print(f"    no entry names the last file ({filename or 'none'}), so the ticks are not compared")


async def run(args: argparse.Namespace) -> int:
    import aiohttp

    from custom_components.generic_3dprinter import discovery
    from custom_components.generic_3dprinter.adapters import sdcp
    from custom_components.generic_3dprinter.const import Command, ProtocolId
    from custom_components.generic_3dprinter.protocols import (
        ConfigError,
        ProtocolError,
        UnsupportedCommandError,
        parse_config,
    )
    from custom_components.generic_3dprinter.registry import build_adapter, get_registration

    report = Report()
    saved: dict[str, Any] = {}

    report.section(f"SDCP discovery on {args.host}:{sdcp.SDCP_DISCOVERY_PORT}")
    answer = await discovery.async_probe_udp(
        sdcp.SDCP_DISCOVERY_PROBE, (args.host, sdcp.SDCP_DISCOVERY_PORT), args.timeout
    )
    saved["udp"] = answer[0] if answer is not None else None
    identity = sdcp.sdcp_identity(answer[0]) if answer is not None else {}
    if report.check("the printer answered the discovery request", answer is not None):
        _show("reply", answer[0])  # type: ignore[index]
    kind = sdcp.classify_sdcp(identity)
    report.check("it answers as a resin printer", kind == "resin", f"{identity.get('MachineName')}: {kind}")
    if kind == "fdm":
        print("    stopping: this tool is for resin printers only")
        return _finish(report, args, saved, [])
    version = str(identity.get("ProtocolVersion") or "")
    if answer is not None:
        report.check("it speaks SDCP V3", not version.upper().startswith("V1"), version)

    report.section("TCP ports")
    ports = sorted({*PORTS, args.port})
    open_ports = set(await discovery.async_probe_ports(args.host, ports))
    saved["ports"] = {str(port): port in open_ports for port in ports}
    for port in ports:
        print(f"    {port}: {'open' if port in open_ports else 'closed'}")
    if not report.check("the SDCP port accepts connections", args.port in open_ports, str(args.port)):
        return _finish(report, args, saved, [])

    sent: list[int] = []
    async with aiohttp.ClientSession() as session:
        report.section("setup through the registry")
        registration = get_registration(ProtocolId.SDCP_RESIN)
        config = parse_config(
            {
                "name": "acceptance",
                "protocol": registration.id.value,
                "host": args.host,
                "port": args.port,
                "serial": args.serial or None,
            }
        )
        try:
            config = await registration.adapter.async_prepare_config(config)
        except ConfigError as err:
            report.check("the entry would be accepted", False, str(err))
            return _finish(report, args, saved, sent)
        report.check("the entry would be accepted", True, f"mainboard id {config.serial}")
        adapter = build_adapter(config, session)
        sent = _record_commands(adapter)
        kinds = _record_topics(adapter)
        try:
            try:
                await adapter.async_setup()
            except ProtocolError as err:
                report.check("connected and checked the printer", False, f"{type(err).__name__}: {err}")
                return _finish(report, args, saved, sent)
            report.check("connected and checked the printer", True, adapter.ws_url)
            profile = adapter.model_profile
            report.check("the model has a profile", profile is not None, profile.name if profile else str(adapter.model_id))
            saved["attributes"] = dict(adapter.attributes)
            _show("attributes", saved["attributes"])

            report.section("snapshot")
            snapshot = await adapter.async_read()
            saved["status"] = dict(adapter._status)  # noqa: SLF001
            _show("status", saved["status"])
            _show("snapshot", snapshot.as_dict(), limit=6000)
            check_snapshot(report, snapshot)

            report.section("file list")
            files = await adapter.async_list_files()
            saved["files"] = [item.path for item in files]
            report.check("the file list was read", True, f"{len(files)} files")
            for item in files[:10]:
                print(f"    {item.path} ({item.size} bytes)")

            report.section("history")
            await check_history(report, adapter, saved)

            report.section("a command this printer must never get")
            before = len(sent)
            try:
                await adapter.async_send(Command.SET_HOTEND_TEMP, value=50)
            except UnsupportedCommandError as err:
                report.check("a nozzle target is refused", True, str(err))
            except ProtocolError as err:
                report.check("a nozzle target is refused", False, f"{type(err).__name__}: {err}")
            else:
                report.check("a nozzle target is refused", False, "it was accepted")
            report.check("nothing was sent for it", sent[before:] == [], str(sent[before:]))

            if args.keepalive:
                report.section(f"holding the socket {args.keepalive:g} s on the status heartbeat")
                ws, before = adapter._ws, len(sent)  # noqa: SLF001
                await asyncio.sleep(args.keepalive)
                report.check("the same socket is still open", adapter._ws is ws and adapter._connected)  # noqa: SLF001
                report.check("only the status was sent meanwhile", set(sent[before:]) <= {0}, str(sent[before:]))
                snapshot = await adapter.async_read()
                report.check("a read after the wait needs no new socket", adapter._ws is ws and snapshot.connected)  # noqa: SLF001

            if args.watch:
                report.section(f"watching {args.watch:g} s, the status every {args.interval:g} s")
                await _watch(report, adapter, args.watch, args.interval)
        finally:
            await adapter.async_teardown()
    print(f"    frames received by kind: {dict(kinds)}")
    return _finish(report, args, saved, sent)


async def _watch(report: Report, adapter: Any, seconds: float, interval: float) -> None:
    """Ask for the status every ``interval`` seconds and print a line when it changes."""
    from custom_components.generic_3dprinter.protocols import ProtocolError

    started, last, reads = time.monotonic(), None, 0
    connected = True
    while time.monotonic() - started < seconds:
        try:
            await adapter._async_refresh_status()  # noqa: SLF001
            snapshot = await adapter.async_read()
        except ProtocolError as err:
            report.check("the status was read throughout", False, f"after {reads} reads: {err}")
            return
        reads += 1
        connected = connected and snapshot.connected
        resin = snapshot.resin
        info = adapter._status.get("PrintInfo") or {}  # noqa: SLF001
        line = (
            snapshot.print_state.value,
            resin.phase.value if resin and resin.phase else None,
            resin.phase_code if resin else None,
            f"{info.get('CurrentLayer')}/{info.get('TotalLayer')}",
            resin.release_film if resin else None,
            resin.vat_heat_status if resin else None,
            info.get("TaskId"),
        )
        if line != last:
            print(f"    {time.monotonic() - started:7.1f}s  state {line[0]}, phase {line[1]} ({line[2]}), "
                  f"layer {line[3]}, film {line[4]}, heat {line[5]}, task {line[6]!r}")
            last = line
        await asyncio.sleep(interval)
    report.check("the status was read throughout", connected and reads > 0, f"{reads} reads")


def _finish(report: Report, args: argparse.Namespace, saved: dict[str, Any], sent: list[int]) -> int:
    """Check that only reads went out, save what was read, and return the exit code."""
    report.check("only read commands were sent", set(sent) <= READS, str(sorted(set(sent))))
    saved["sent"] = sent
    if args.save:
        Path(args.save).write_bytes(json.dumps(saved, indent=2, default=str).encode("utf-8"))
        print(f"    saved to {args.save}")
    return report.summary()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("host", help="the printer's address")
    parser.add_argument("--port", type=int, default=3030, help="the SDCP WebSocket port")
    parser.add_argument("--serial", help="the mainboard id, when the printer does not answer discovery")
    parser.add_argument("--save", metavar="FILE", help="save every raw reply read to FILE as JSON")
    parser.add_argument("--timeout", type=float, default=3.0, help="seconds to wait for the discovery reply")
    parser.add_argument("--keepalive", type=float, default=0.0, help="hold the socket N seconds on the status heartbeat alone")
    parser.add_argument("--watch", type=float, default=0.0, help="print the status as it changes, N seconds")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between status reads in --watch")
    args = parser.parse_args()
    if sys.platform == "win32":
        # aiodns refuses the Proactor loop that Windows defaults to.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
