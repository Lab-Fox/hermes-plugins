"""NanoGPT image generation backend.

Uses NanoGPT's normalized Image API — ``POST https://api.nano-gpt.com/api/v1/images``
— rather than the OpenAI-compatible ``/v1/images/generations`` route, because the
normalized route takes semantic ``aspect_ratio`` values and accepts reference
images as plain URLs (the OpenAI-compatible route requires base64 data URLs).

Response shape is OpenAI-like: ``{"data": [{"b64_json"|"url"}], "cost": …,
"remainingBalance": …}``.

Model selection: ``model`` kwarg → ``NANOGPT_IMAGE_MODEL`` →
``image_gen.nanogpt.model`` → ``image_gen.model`` → :data:`DEFAULT_MODEL`.
Unknown ids are passed through verbatim, so any NanoGPT image model works.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO, resolve_aspect_ratio, save_b64_image, save_url_image,
    success_response)
from agent.secret_scope import get_secret, get_secret_str
from plugins.image_gen._common import (
    StaticImageGenProvider, collect_source_images, error_factory, prompt_required_error,
    resolve_static_model)

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_URL = "https://api.nano-gpt.com/api/v1/images"
IMAGE_URL_ENV = "NANOGPT_IMAGE_URL"
# Image-specific key first: a subscription-billed key keeps image generation on
# the plan's image quota instead of spending the pay-as-you-go balance. Falls
# back to the chat credential when no separate image key is configured.
API_KEY_ENVS = ("NANOGPT_IMAGE_API_KEY", "NANOGPT_API_KEY", "HERMES_CUSTOM_NANOGPT_API_KEY")
API_KEY_ENV = "NANOGPT_IMAGE_API_KEY"
MODEL_ENV = "NANOGPT_IMAGE_MODEL"
REQUEST_TIMEOUT = 240.0

# Semantic aspect → the model's own vocabulary. ``param`` says which body field
# carries it: models that publish ``aspect_ratio`` take ``"16:9"``-style values,
# older ones only take a fixed ``resolution`` such as ``"1024x768"``.
_ASPECT_16_9 = {"landscape": "16:9", "square": "1:1", "portrait": "9:16"}

_MODELS: dict[str, dict[str, Any]] = {
    # --- uncensored / NSFW-capable, cheapest tier (~$0.005 per 1024px image) ---
    "cyberrealistic-xl": {
        "api_model": "cyberrealistic-xl",
        "param": "resolution",
        "aspect": {"landscape": "1920x1088", "square": "1024x1024", "portrait": "1088x1920"},
        "max_refs": 1,
        "display": "CyberRealistic XL (uncensored)",
        "speed": "~8s",
        "strengths": "Photoreal SDXL checkpoint, no safety filtering — cheapest usable default",
        "price": "$0.005/image (1024²)",
    },
    "wai-illustrious-sdxl": {
        "api_model": "wai-illustrious-sdxl",
        "param": "resolution",
        "aspect": {"landscape": "1920x1088", "square": "1024x1024", "portrait": "1088x1920"},
        "max_refs": 1,
        "display": "WAI Illustrious SDXL (uncensored, anime)",
        "speed": "~8s",
        "strengths": "Illustration/anime SDXL checkpoint with no safety filtering",
        "price": "$0.005/image (1024²)",
    },
    "hassaku-hentai": {
        "api_model": "hassaku-hentai",
        "param": "resolution",
        "aspect": {"landscape": "1920x1088", "square": "1024x1024", "portrait": "1088x1920"},
        "max_refs": 1,
        "display": "Moxie Diffusion XL (explicit)",
        "speed": "~8s",
        "strengths": "NSFW-specialised SDXL checkpoint built for adult content",
        "price": "$0.005/image (1024²)",
    },
    "qwen-image-3-uncensored": {
        "api_model": "qwen-image-3-uncensored",
        "param": "aspect_ratio", "aspect": _ASPECT_16_9, "resolution": "1k", "max_refs": 0,
        "display": "Qwen Image 3 Uncensored",
        "speed": "~20s",
        "strengths": "Modern uncensored model, native aspect ratios, 1K/2K, long prompts",
        "price": "$0.04/image",
    },
    "qwen-edit-uncensored": {
        "api_model": "qwen-edit-uncensored",
        "param": "aspect_ratio", "aspect": _ASPECT_16_9, "resolution": "1k", "max_refs": 3,
        "display": "Qwen Edit Uncensored",
        "speed": "~20s",
        "strengths": "Instruction-based image editing (uncensored) — use this to change an existing image",
        "price": "$0.04/image",
    },
    # --- general purpose ---
    "krea-2-turbo": {
        "api_model": "krea-v2/turbo",
        "param": "aspect_ratio", "aspect": _ASPECT_16_9, "resolution": "1k", "max_refs": 4,
        "display": "Krea 2 Turbo",
        "speed": "~10s",
        "strengths": "Cheapest usable default — fast 1K/2K, native aspect ratios",
        "price": "$0.012/image (1K)",
    },
    "seedream-5.5": {
        "api_model": "bytedance/seedream-v5.5",
        "param": "aspect_ratio", "aspect": _ASPECT_16_9, "resolution": "1k", "max_refs": 10,
        "display": "Seedream 5.5 (ByteDance)",
        "speed": "~15s",
        "strengths": "Strong prompt following, 15 aspect ratios, up to 3K, 10 references",
        "price": "$0.02/image (1K)",
    },
    "qwen-image-2.1": {
        "api_model": "qwen-image-2.1/text-to-image",
        "param": "aspect_ratio", "aspect": _ASPECT_16_9, "resolution": "1k", "max_refs": 3,
        "display": "Qwen Image 2.1",
        "speed": "~15s",
        "strengths": "Multilingual text rendering, up to 2K",
        "price": "$0.02/image (1K)",
    },
    "nano-banana-2": {
        "api_model": "nano-banana-2",
        "param": "aspect_ratio", "aspect": _ASPECT_16_9, "resolution": "1k", "max_refs": 14,
        "display": "Nano Banana 2 (Google)",
        "speed": "~20s",
        "strengths": "Gemini 3.1 Flash Image — best all-round quality, edits, up to 4K",
        "price": "$0.08/image (1K)",
    },
    "gpt-image-2.6-flare": {
        "api_model": "gpt-image-2.6-flare",
        "param": "aspect_ratio", "aspect": _ASPECT_16_9, "resolution": "1k", "max_refs": 16,
        "display": "GPT Image 2.6 Flare",
        "speed": "~40s",
        "strengths": "OpenAI image model — precise text rendering, 16 references",
        "price": "$0.09/image (1K)",
    },
    "nano-banana-pro": {
        "api_model": "nano-banana-pro",
        "param": "aspect_ratio", "aspect": _ASPECT_16_9, "resolution": "1k", "max_refs": 10,
        "display": "Nano Banana Pro (Google)",
        "speed": "~25s",
        "strengths": "High-res text + edit model, mobile-friendly 4K",
        "price": "$0.14/image (1K)",
    },
    "flux-3-klein": {
        "api_model": "blackforestlabs/flux-3/klein/text-to-image",
        "param": "resolution",
        "aspect": {"landscape": "1536x1536", "square": "1024x1024", "portrait": "1536x1536"},
        "max_refs": 10,
        "display": "FLUX.3 Klein (BFL)",
        "speed": "~8s",
        "strengths": "Cheapest per image, precise layout and typography (square output only)",
        "price": "$0.009/image (1024²)",
    },
    "recraft-v4": {
        "api_model": "recraft-v4",
        "param": "resolution",
        "aspect": {"landscape": "1024x768", "square": "1024x1024", "portrait": "768x1024"},
        "max_refs": 0,
        "display": "Recraft V4",
        "speed": "~20s",
        "strengths": "Design and marketing work — flat vector-friendly output",
        "price": "$0.04/image",
    },
}
DEFAULT_MODEL = "cyberrealistic-xl"


def _resolve_api_key() -> Optional[str]:
    """First non-empty auth env var, in priority order."""
    return next((val for val in map(get_secret, API_KEY_ENVS) if val), None)


def _resolve_image_url() -> str:
    # Through the secret scope: under multiplexing os.environ may hold another
    # profile's endpoint, and a routed profile's key must not go to it.
    return get_secret_str(IMAGE_URL_ENV).strip() or DEFAULT_IMAGE_URL


def _resolve_model(caller_model: Optional[str] = None) -> tuple[str, dict[str, Any]]:
    return resolve_static_model(
        _MODELS, DEFAULT_MODEL, env_var=MODEL_ENV, config_key="nanogpt",
        explicit=caller_model, passthrough=True)


def _sniff_extension(b64: str) -> str:
    """File extension from the base64 magic bytes (models return png/jpg/webp)."""
    head = b64[:16]
    if head.startswith("/9j/"):
        return "jpg"
    if head.startswith("UklGR"):
        return "webp"
    if head.startswith("R0lGOD"):
        return "gif"
    return "png"


class NanoGPTImageGenProvider(StaticImageGenProvider):
    """NanoGPT normalized Image API backend."""

    provider_id = "nanogpt"
    label = "NanoGPT"
    models = _MODELS
    default_model_id = DEFAULT_MODEL
    setup = dict(
        name="NanoGPT", badge="paid", tag="Image generation billed to your NanoGPT balance",
        key=API_KEY_ENV, prompt="NanoGPT image key (use a subscription-billed key to spend image quota, not balance)",
        url="https://nano-gpt.com/api")

    def is_available(self) -> bool:
        return bool(_resolve_api_key())

    def capabilities(self) -> dict[str, Any]:
        return {"modalities": ["text", "image"], "max_reference_images": 16}

    def generate(
        self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *,
        image_url: Optional[str] = None, reference_image_urls: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return prompt_required_error("nanogpt", aspect)
        api_key = _resolve_api_key()
        if not api_key:
            return error_factory("nanogpt", aspect)(
                f"{API_KEY_ENV} not set. Add it to .env or run `hermes tools` -> "
                "Image Generation -> NanoGPT to configure.", "auth_required")

        model_id, meta = _resolve_model(kwargs.get("model"))
        fail = error_factory("nanogpt", aspect, model=model_id, prompt=prompt)

        aspect_map = meta.get("aspect") or _ASPECT_16_9
        body: dict[str, Any] = {"model": meta.get("api_model") or model_id, "prompt": prompt, "n": 1}
        if meta.get("param") == "resolution":
            body["resolution"] = aspect_map.get(aspect, aspect_map["square"])
        else:
            body["aspect_ratio"] = aspect_map.get(aspect, aspect_map["square"])
            if meta.get("resolution"):
                body["resolution"] = meta["resolution"]

        max_refs = meta.get("max_refs")
        sources = collect_source_images(
            image_url, reference_image_urls,
            limit=max_refs if isinstance(max_refs, int) else None)
        if sources:
            body["input_references"] = sources

        try:
            import httpx
        except ImportError:
            return fail("httpx is not installed (pip install httpx)", "missing_dependency")

        try:
            response = httpx.post(
                _resolve_image_url(), json=body, timeout=REQUEST_TIMEOUT,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        except Exception as exc:
            logger.debug("NanoGPT image request failed", exc_info=True)
            return fail(f"NanoGPT image request failed: {exc}", "api_error")

        if response.status_code != 200:
            detail = ""
            try:
                payload = response.json()
                detail = str(payload.get("error") or payload.get("message") or payload)[:300]
            except Exception:
                detail = (response.text or "")[:300]
            return fail(f"NanoGPT returned HTTP {response.status_code}: {detail}", "api_error")

        try:
            payload = response.json()
            first = (payload.get("data") or [])[0]
        except (ValueError, IndexError, AttributeError, TypeError):
            return fail("NanoGPT response contained no image data", "empty_response")

        b64 = first.get("b64_json")
        url = first.get("url")
        try:
            if b64:
                image_ref = str(save_b64_image(b64, prefix="nanogpt", extension=_sniff_extension(b64)))
            elif url:
                image_ref = str(save_url_image(url, prefix="nanogpt"))
            else:
                return fail("NanoGPT response contained neither b64_json nor a URL", "empty_response")
        except Exception as exc:
            return fail(f"Failed to save NanoGPT image: {exc}", "io_error")

        extra: dict[str, Any] = {}
        for key in ("cost", "remainingBalance", "requestId"):
            if payload.get(key) is not None:
                extra[key] = payload[key]
        return success_response(
            image=image_ref, model=model_id, prompt=prompt, aspect_ratio=aspect,
            provider="nanogpt", modality="image" if sources else "text", extra=extra)


def register(ctx) -> None:
    """Plugin entry point — wire ``NanoGPTImageGenProvider`` into the registry."""
    ctx.register_image_gen_provider(NanoGPTImageGenProvider())
