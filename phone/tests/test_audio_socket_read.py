"""AudioSocket TLV reader — cancellation safety (2026-09-07, PH3).

Every caller polls ``read_frame`` through ``asyncio.wait_for``; a timeout
that landed between the header read and the payload read used to consume
the header and desync the stream for the rest of the call (three payload
bytes read as the next header → a random length → the call goes deaf). The
header is now parked on the connection across a cancelled payload wait.
"""

import asyncio
import struct

import pytest

from telephony.audio_socket import (
    HEADER_SIZE, TYPE_AUDIO, TYPE_HANGUP, AudioSocketConnection, AudioSocketError,
)


class _DummyWriter:
    def get_extra_info(self, name, default=None):
        return default

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


def _frame(kind: int, payload: bytes) -> bytes:
    return struct.pack(">BH", kind, len(payload)) + payload


@pytest.mark.asyncio
async def test_timeout_between_header_and_payload_keeps_the_stream_in_sync():
    reader = asyncio.StreamReader()
    conn = AudioSocketConnection(reader=reader, writer=_DummyWriter())
    audio = bytes(range(256)) + b"\x00" * 64                 # 320 bytes
    # Header only → the payload wait times out (the listen loop's poll).
    reader.feed_data(struct.pack(">BH", TYPE_AUDIO, len(audio)))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(conn.read_frame(), timeout=0.02)
    assert conn._pending_header is not None
    # The payload arrives later, then a whole second frame.
    reader.feed_data(audio)
    reader.feed_data(_frame(TYPE_HANGUP, b""))
    assert await conn.read_frame() == (TYPE_AUDIO, audio)
    assert conn._pending_header is None
    assert await conn.read_frame() == (TYPE_HANGUP, b"")


@pytest.mark.asyncio
async def test_repeated_timeouts_before_any_byte_lose_nothing():
    reader = asyncio.StreamReader()
    conn = AudioSocketConnection(reader=reader, writer=_DummyWriter())
    for _ in range(3):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(conn.read_frame(), timeout=0.01)
    assert conn._pending_header is None
    reader.feed_data(_frame(TYPE_AUDIO, b"\x01" * 320))
    assert await conn.read_frame() == (TYPE_AUDIO, b"\x01" * 320)


@pytest.mark.asyncio
async def test_outgoing_dump_records_the_first_seconds(tmp_path, monkeypatch):
    """OTODOCK_PHONE_DUMP_OUTGOING_S=N records exactly the first N seconds the
    daemon hands Asterisk, as a WAV under BASE_DIR/debug, then stops."""
    import wave
    from telephony import audio_socket as mod

    monkeypatch.setenv("OTODOCK_PHONE_DUMP_OUTGOING_S", "0.05")   # 800 bytes
    monkeypatch.setattr(mod.config, "BASE_DIR", tmp_path)
    conn = AudioSocketConnection(reader=asyncio.StreamReader(), writer=_DummyWriter())
    conn.call_uuid = "abc"
    for i in range(5):
        conn.send_audio(bytes([i]) * 320)
    path = tmp_path / "debug" / "abc-out.wav"
    assert path.exists()
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2)
        data = w.readframes(w.getnframes())
    assert len(data) == 800 and data[:320] == b"\x00" * 320 and data[640:] == b"\x02" * 160
    await conn.close()                                   # idempotent close
    # Off by default: no file.
    monkeypatch.delenv("OTODOCK_PHONE_DUMP_OUTGOING_S")
    conn2 = AudioSocketConnection(reader=asyncio.StreamReader(), writer=_DummyWriter())
    conn2.call_uuid = "def"
    conn2.send_audio(b"\x00" * 320)
    assert not (tmp_path / "debug" / "def-out.wav").exists()


@pytest.mark.asyncio
async def test_connection_closed_paths_still_raise_and_clear_the_header():
    reader = asyncio.StreamReader()
    conn = AudioSocketConnection(reader=reader, writer=_DummyWriter())
    reader.feed_data(struct.pack(">BH", TYPE_AUDIO, 320) + b"\x00" * 10)
    reader.feed_eof()
    with pytest.raises(AudioSocketError):
        await conn.read_frame()                      # closed during the payload
    assert conn._pending_header is None
    reader2 = asyncio.StreamReader()
    conn2 = AudioSocketConnection(reader=reader2, writer=_DummyWriter())
    reader2.feed_data(b"\x10")
    reader2.feed_eof()
    with pytest.raises(AudioSocketError):
        await conn2.read_frame()                     # closed during the header
    assert HEADER_SIZE == 3
