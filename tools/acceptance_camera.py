"""Prove a printer's camera delivers a continuous live stream.

The unit suite proves the plumbing against a fake. This runs the same code path
against real hardware and reports how many frames arrived and at what rate, so
"the camera is live" is a measurement rather than a claim.

    python tools/acceptance_camera.py 192.168.128.143

Read-only. It pulls one upstream connection for the whole run, exactly as the
integration does, so it can be run while a print is going.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from acceptance_kit import Report

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

if sys.platform == "win32":
    # aiodns refuses the Proactor loop that Windows defaults to.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def run(host: str, seconds: float, camera_out: Path | None) -> int:
    import aiohttp

    from generic_3dprinter.adapters import sdcp

    config_host = host
    camera_url = f"http://{config_host}:{sdcp.DEFAULT_CAMERA_PORT}{sdcp.CAMERA_PATH}"
    print(f"[1] camera: {camera_url}")

    frames: list[bytes] = []
    times: list[float] = []
    started = time.monotonic()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
        try:
            async with session.get(
                camera_url, timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=10)
            ) as response:
                print(f"    status {response.status}, type {response.headers.get('Content-Type')}")
                if response.status >= 400:
                    print("    camera refused the connection")
                    return 1

                buffer = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    buffer.extend(chunk)
                    for frame in sdcp.jpeg_frames(buffer):
                        frames.append(frame)
                        times.append(time.monotonic())
                    if time.monotonic() - started >= seconds:
                        break
        except (aiohttp.ClientError, TimeoutError) as err:
            print(f"    camera failed: {type(err).__name__}: {err}")
            return 1

    elapsed = time.monotonic() - started
    distinct = len({bytes(frame) for frame in frames})
    print(f"[2] {len(frames)} frames in {elapsed:.1f}s ({len(frames) / elapsed:.1f} fps)")
    print(f"    {distinct} distinct payloads")
    print(f"    sizes: min={min(len(f) for f in frames)} max={max(len(f) for f in frames)}")

    report = Report()
    check = report.check

    check("more than one frame arrived", len(frames) > 1, f"{len(frames)}")
    check("the frames are all distinct", distinct == len(frames), f"{distinct}/{len(frames)}")
    check("every frame is a complete JPEG", all(f[:2] == b"\xff\xd8" and f[-2:] == b"\xff\xdf" or f[-2:] == b"\xff\xd9" for f in frames))
    check("the stream sustained at least 2 fps", len(frames) / elapsed >= 2.0, f"{len(frames) / elapsed:.1f} fps")

    if camera_out is not None and frames:
        camera_out.write_bytes(frames[-1])
        print(f"[3] wrote the last frame to {camera_out}")

    return report.summary()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--camera", type=Path, default=None)
    args = parser.parse_args()
    return asyncio.run(run(args.host, args.seconds, args.camera))


if __name__ == "__main__":
    raise SystemExit(main())
