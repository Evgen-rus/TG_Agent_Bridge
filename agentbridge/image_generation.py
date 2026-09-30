from __future__ import annotations

import base64
import binascii
from typing import Protocol

from openai import AsyncOpenAI


MAX_GENERATED_IMAGE_BYTES = 10 * 1024 * 1024


class ImageGenerationError(RuntimeError):
    """An image could not be generated or was not returned in a supported form."""


class ImageGenerator(Protocol):
    async def generate(self, prompt: str) -> bytes: ...


class OpenAIImageGenerator:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-image-2.5-flare",
        size: str = "1024x1024",
        quality: str = "low",
        client=None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.size = size
        self.quality = quality
        self._client = client

    async def generate(self, prompt: str) -> bytes:
        if not self._api_key and self._client is None:
            raise ImageGenerationError("Image generation is not configured")
        if not prompt.strip():
            raise ImageGenerationError("Image prompt is empty")
        try:
            async def request(client):
                return await client.images.generate(
                    model=self.model,
                    prompt=prompt,
                    n=1,
                    size=self.size,
                    quality=self.quality,
                    output_format="jpeg",
                )

            if self._client is None:
                async with AsyncOpenAI(api_key=self._api_key, timeout=150.0) as client:
                    response = await request(client)
            else:
                response = await request(self._client)
            encoded = response.data[0].b64_json
            image = base64.b64decode(encoded, validate=True)
        except (AttributeError, IndexError, TypeError, ValueError, binascii.Error) as exc:
            raise ImageGenerationError("Image generation returned invalid image data") from exc
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise ImageGenerationError("Image generation request failed") from exc
        if not image or len(image) > MAX_GENERATED_IMAGE_BYTES or not image.startswith(b"\xff\xd8"):
            raise ImageGenerationError("Image generation returned an unsupported image")
        return image
