"""Entry point: python3 -m netflow [--config /etc/netflow/netflow.json]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from pathlib import Path
from typing import List, Tuple

from . import portal
from .app import Conf, Controller
from .dnsproxy import DnsProxy
from .nft import Nft
from .notifier import Notifier

log = logging.getLogger("netflow")

async def main(conf: Conf) -> None:
    nft = Nft()
    ctl = Controller(conf, nft)
    notifier = Notifier(conf.tvoverlay_port, conf.notify_duration_s)
    # Push the restored rules before anything else, so a device that was
    # blocked before the restart is still blocked on its first packet.
    ctl.sync()

    async def on_video(client_ip: str, addrs: List[Tuple[str, int]]) -> None:
        fresh = ctl.learn_video(client_ip, addrs)
        if fresh:
            await nft.add_video(fresh)

    proxy = DnsProxy(
        conf.upstream_dns,
        ctl.is_yt_blocked,
        on_video,
        on_query=ctl.note_query,
        youtube_domains=conf.youtube_domains,
        video_domains=conf.video_domains,
        enc_dns_blocked=ctl.enc_dns_blocked,
        enc_dns_domains=conf.enc_dns_domains,
    )
    await portal.serve(ctl, conf.http_port)

    async def dns_forever() -> None:
        # The TV-side interface may come up after us: keep trying to bind.
        while True:
            try:
                await proxy.serve(conf.lan_ip)
                return
            except OSError as e:
                log.warning("DNS bind on %s failed (%s), retrying", conf.lan_ip, e)
                await asyncio.sleep(5)

    asyncio.get_running_loop().create_task(dns_forever())

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)

    next_tick = time.monotonic()
    while not stop.is_set():
        try:
            ctl.tick()
        except Exception:  # one bad tick must not take enforcement down
            log.exception("tick failed")
        # Delivered in the background: a TV that does not answer must not
        # hold up the next tick.
        for n in ctl.drain_outbox():
            asyncio.get_running_loop().create_task(notifier.send(n))
        next_tick += 1.0
        delay = next_tick - time.monotonic()
        if delay < 0:  # fell behind (suspend, long stall): resync, do not burst
            next_tick = time.monotonic()
            delay = 0
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except asyncio.TimeoutError:
            pass
    ctl.save()
    log.info("stopped, state saved")


def run() -> None:
    ap = argparse.ArgumentParser(prog="netflow")
    ap.add_argument("--config", default="/etc/netflow/netflow.json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(main(Conf.load(Path(args.config))))


if __name__ == "__main__":
    run()
