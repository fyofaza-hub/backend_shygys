import asyncio
import io
from types import SimpleNamespace

from botocore.response import StreamingBody

from app.media import preview_response


def test_botocore_stream_is_read_and_closed_without_entering_raw_response(monkeypatch):
    payload = b"streamed image" * 10000
    raw = io.BytesIO(payload)
    stream = StreamingBody(raw, len(payload))
    client = SimpleNamespace(get_object=lambda **kwargs: {"Body": stream})
    monkeypatch.setattr("app.media.s3", lambda: client)

    async def consume():
        response = await preview_response("image", "image/jpeg")
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(consume()) == payload
    assert raw.closed
