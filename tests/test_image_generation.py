from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentbridge.image_generation import ImageGenerationError, OpenAIImageGenerator


@pytest.mark.asyncio
async def test_image_generator_requests_one_jpeg_and_decodes_response() -> None:
    jpeg = b"\xff\xd8mock-image\xff\xd9"
    generate = AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(
        b64_json=base64.b64encode(jpeg).decode("ascii"),
    )]))
    client = SimpleNamespace(images=SimpleNamespace(generate=generate))
    generator = OpenAIImageGenerator("test-key", client=client)

    result = await generator.generate("A smiling robot named Rick")

    assert result == jpeg
    generate.assert_awaited_once_with(
        model="gpt-image-2.5-flare", prompt="A smiling robot named Rick", n=1,
        size="1024x1024", quality="low", output_format="jpeg",
    )


@pytest.mark.asyncio
async def test_image_generator_rejects_missing_or_invalid_response() -> None:
    missing = OpenAIImageGenerator("")
    with pytest.raises(ImageGenerationError, match="not configured"):
        await missing.generate("draw Rick")

    client = SimpleNamespace(images=SimpleNamespace(
        generate=AsyncMock(return_value=SimpleNamespace(data=[])),
    ))
    with pytest.raises(ImageGenerationError, match="invalid image data"):
        await OpenAIImageGenerator("test-key", client=client).generate("draw Rick")
