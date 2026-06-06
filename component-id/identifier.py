"""LLM layer -- the ONLY place that talks to Claude (encapsulated).

Stable interface:  identify(image_bytes, dmm=None) -> card (dict).
- With ANTHROPIC_API_KEY (.env) + anthropic SDK installed: real Claude Vision ID
  (Messages API, image via base64, forced tool schema -> guaranteed JSON).
- Without key/SDK: falls back to a stub card (service stays operational, with a note).

Image alone is sufficient; dmm is an optional additional anchor. The API key stays on the service.
"""


from __future__ import annotations

import base64
import io

from card import build_stub_card, empty_card
from config import get_api_key, get_model
from prompt import CARD_TOOL, SYSTEM_PROMPT, build_user_text

# Anthropic recommends max ~1568 px on the long edge; larger images are server-side
# downscaled anyway and waste image tokens (token ~ W*H/750). Pre-scaling here
# saves tokens/cost/time and avoids TPM rate limits.
_MAX_EDGE = 1568


def _media_type(b: bytes) -> str:
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def _downscale(image_bytes: bytes) -> tuple[bytes, str]:
    """Downscales the image to at most _MAX_EDGE on the long edge; returns (bytes, media_type).
    On error or without Pillow: returns original bytes unchanged."""
    try:
        from PIL import Image
    except ImportError:
        return image_bytes, _media_type(image_bytes)
    try:
        im = Image.open(io.BytesIO(image_bytes))
        im = im.convert("RGB")
        w, h = im.size
        scale = _MAX_EDGE / max(w, h)
        if scale < 1.0:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, _media_type(image_bytes)


def _stub(image_bytes: bytes, dmm: dict | None, reason: str) -> dict:
    card = build_stub_card(image_bytes, dmm)
    card["hinweis"] = f"{reason} -> stub response. (image={len(image_bytes)} B, DMM={'yes' if dmm else 'no'})"
    return card


def identify(image_bytes: bytes, dmm: dict | None = None) -> dict:
    """Identifies a component from the image. dmm is an optional additional anchor."""
    key = get_api_key()
    if not key:
        return _stub(image_bytes, dmm, "ANTHROPIC_API_KEY missing (.env)")
    try:
        from anthropic import Anthropic
    except ImportError:
        return _stub(image_bytes, dmm, "anthropic package not installed")

    client = Anthropic(api_key=key)
    send_bytes, media_type = _downscale(image_bytes)
    img_b64 = base64.b64encode(send_bytes).decode("ascii")

    try:
        resp = client.messages.create(
            model=get_model(),
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[CARD_TOOL],
            tool_choice={"type": "tool", "name": "report_component"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": img_b64,
                    }},
                    {"type": "text", "text": build_user_text(dmm)},
                ],
            }],
        )
    except Exception as e:  # network/auth/rate-limit etc. -- service must not crash
        return _stub(image_bytes, dmm, f"Claude call failed: {type(e).__name__}: {e}")

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "report_component":
            card = empty_card()
            card.update(block.input)  # validated by schema
            card["_model"] = get_model()
            return card

    return _stub(image_bytes, dmm, "no tool_use response from Claude")
