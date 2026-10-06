"""On-screen reminders through TvOverlay, an Android TV app that shows a
notification for a JSON POST on its port (https://github.com/gugutab/TvOverlay).

TvOverlay offers no text size, so the reminder is drawn as a large-type PNG
and sent as the image alone -- no title or message, whose small type spoils
the picture. (TvOverlay still adds a small "REST API" line of its own, which
cannot be removed from outside.) Without Pillow or the font it falls back to
plain text, so a reminder is never lost. The TVs sit on the router's own network, so this reaches
them even while their internet is cut."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Optional, Set

log = logging.getLogger(__name__)

FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_INDEX_TC = 2  # Traditional Chinese face in the Noto CJK collections


@dataclass
class Notice:
    ip: str
    big: str  # the headline, drawn large
    sub: str  # one line of context under it

    @property
    def text(self) -> str:
        return f"{self.big}：{self.sub}"


def render_png(big: str, sub: str) -> Optional[bytes]:
    try:
        from PIL import Image, ImageDraw, ImageFont

        w, h = 960, 300
        img = Image.new("RGB", (w, h), (25, 28, 34))
        d = ImageDraw.Draw(img)
        d.text((w // 2, 110), big, font=ImageFont.truetype(FONT_BOLD, 110, index=FONT_INDEX_TC),
               fill=(255, 196, 0), anchor="mm")
        d.text((w // 2, 230), sub, font=ImageFont.truetype(FONT_REGULAR, 52, index=FONT_INDEX_TC),
               fill=(230, 230, 230), anchor="mm")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except (ImportError, OSError) as e:
        log.warning("cannot draw the reminder image (%s): sending text only", e)
        return None


def _post(url: str, body: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


class Notifier:
    def __init__(self, port: int, duration_s: int) -> None:
        self.port = port
        self.duration_s = duration_s
        self._prepared: Set[str] = set()

    async def send(self, n: Notice) -> None:
        loop = asyncio.get_running_loop()
        base = f"http://{n.ip}:{self.port}"
        try:
            if n.ip not in self._prepared:
                # TvOverlay shows a permanent clock by default; turn it off.
                await loop.run_in_executor(None, _post, base + "/set/overlay", {"clockOverlayVisibility": 0})
                # Only the Default layout shows an image-only notification
                # (Minimalist shows nothing, Icon Only wants largeIcon).
                await loop.run_in_executor(None, _post, base + "/set/notifications", {"notificationLayoutName": "Default"})
                self._prepared.add(n.ip)
            png = await loop.run_in_executor(None, render_png, n.big, n.sub)
            if png:
                body = {"image": base64.b64encode(png).decode(), "duration": self.duration_s}
            else:
                body = {"title": "上網時間提醒", "message": n.text, "duration": self.duration_s}
            await loop.run_in_executor(None, _post, base + "/notify", body)
            log.info("reminder to %s: %s", n.ip, n.text)
        except OSError as e:  # TvOverlay not installed / TV asleep: nothing to do
            log.warning("reminder to %s failed: %s", n.ip, e)
