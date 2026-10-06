"""A small forwarding DNS server for the TV network. nftables redirects every
lookup here (even one sent to a hard-coded resolver), so this is where YouTube
is recognised: a device under a YouTube block gets NXDOMAIN for YouTube names,
and the addresses behind googlevideo.com answers are handed to the packet path
so video traffic can be timed and cut by destination."""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from . import dnsmsg

log = logging.getLogger(__name__)

UPSTREAM_TIMEOUT = 2.5

IsBlocked = Callable[[str], bool]  # client ip -> under a YouTube block?
# (client ip, [(video ip, ttl)]): learned per client, so one device's lookup
# does not mark that address as YouTube for every other device.
OnVideo = Callable[[str, List[Tuple[str, int]]], Awaitable[None]]
OnQuery = Callable[[str, str], None]  # (client ip, name) for every query seen
EncDnsBlocked = Callable[[], bool]  # refuse lookups of encrypted-DNS resolvers?


class _OneShot(asyncio.DatagramProtocol):
    def __init__(self, qid: bytes) -> None:
        self.qid = qid
        self.fut: asyncio.Future = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr) -> None:
        if data[:2] == self.qid and not self.fut.done():
            self.fut.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.fut.done():
            self.fut.set_exception(exc)


class DnsProxy:
    def __init__(
        self,
        upstreams: Sequence[str],
        is_blocked: IsBlocked,
        on_video: OnVideo,
        on_query: Optional[OnQuery] = None,
        youtube_domains: Sequence[str] = dnsmsg.YOUTUBE_DOMAINS,
        video_domains: Sequence[str] = dnsmsg.VIDEO_DOMAINS,
        enc_dns_blocked: EncDnsBlocked = lambda: False,
        enc_dns_domains: Sequence[str] = dnsmsg.ENC_DNS_DOMAINS,
    ) -> None:
        self.upstreams = list(upstreams)
        self.is_blocked = is_blocked
        self.on_video = on_video
        self.on_query = on_query
        self.youtube_domains = tuple(youtube_domains)
        self.video_domains = tuple(video_domains)
        self.enc_dns_blocked = enc_dns_blocked
        self.enc_dns_domains = tuple(enc_dns_domains)

    async def answer(self, query: bytes, client_ip: str, tcp: bool = False) -> Optional[bytes]:
        if not dnsmsg.is_query(query):
            return None
        q = dnsmsg.question(query) if dnsmsg.qdcount(query) == 1 else None
        if q is None:
            # Unreadable here, so it could be a YouTube name in disguise (a
            # compressed or second question). Fail closed for a device under
            # a YouTube block; anyone else is forwarded as before.
            if self.is_blocked(client_ip):
                return dnsmsg.formerr(query)
        elif self.on_query:
            self.on_query(client_ip, q[0])
        if q and self.enc_dns_blocked() and dnsmsg.matches(q[0], self.enc_dns_domains):
            return dnsmsg.nxdomain(query)
        if q and dnsmsg.is_youtube_name(q[0], self.youtube_domains) and self.is_blocked(client_ip):
            return dnsmsg.nxdomain(query)
        resp = await (self._forward_tcp(query) if tcp else self._forward_udp(query))
        if resp:
            # A name outside the lists that CNAMEs into YouTube is YouTube too.
            if q and dnsmsg.chain_matches(resp, self.youtube_domains) and self.is_blocked(client_ip):
                return dnsmsg.nxdomain(query)
            vids = dnsmsg.video_addresses(resp, self.video_domains)
            if vids:
                # Before the client sees the answer: its first packet to the
                # video host must already be recognised.
                await self.on_video(client_ip, vids)
        return resp

    async def _forward_udp(self, query: bytes) -> Optional[bytes]:
        loop = asyncio.get_running_loop()
        for up in self.upstreams:
            transport = None
            try:
                transport, proto = await loop.create_datagram_endpoint(lambda: _OneShot(query[:2]), remote_addr=(up, 53))
                transport.sendto(query)
                return await asyncio.wait_for(proto.fut, UPSTREAM_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as e:
                log.warning("upstream %s (udp): %s", up, e or "timeout")
            finally:
                if transport:
                    transport.close()
        return None

    async def _forward_tcp(self, query: bytes) -> Optional[bytes]:
        for up in self.upstreams:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(up, 53), UPSTREAM_TIMEOUT)
                writer.write(struct.pack("!H", len(query)) + query)
                await writer.drain()
                n = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), UPSTREAM_TIMEOUT))[0]
                return await asyncio.wait_for(reader.readexactly(n), UPSTREAM_TIMEOUT)
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
                log.warning("upstream %s (tcp): %s", up, e or "timeout")
            finally:
                if writer:
                    writer.close()
        return None

    async def serve(self, host: str, port: int = 53) -> None:
        loop = asyncio.get_running_loop()
        proxy = self

        class Udp(asyncio.DatagramProtocol):
            def connection_made(self, transport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr) -> None:
                async def reply() -> None:
                    resp = await proxy.answer(data, addr[0])
                    if resp:
                        self.transport.sendto(resp, addr)

                loop.create_task(reply())

        async def tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            peer = writer.get_extra_info("peername")[0]
            try:
                while True:
                    n = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), 30))[0]
                    query = await reader.readexactly(n)
                    resp = await proxy.answer(query, peer, tcp=True)
                    if not resp:
                        break
                    writer.write(struct.pack("!H", len(resp)) + resp)
                    await writer.drain()
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()

        await loop.create_datagram_endpoint(Udp, local_addr=(host, port))
        await asyncio.start_server(tcp_client, host, port)
        log.info("DNS proxy on %s:%d -> %s", host, port, ", ".join(self.upstreams))
