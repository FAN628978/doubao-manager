def sample_mp4_bytes(size: int = 4096) -> bytes:
    """Return deterministic MP4 container bytes for offline tests."""
    header = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
    if size < len(header):
        raise ValueError("size must be at least the MP4 header length")
    return header + b"\x00" * (size - len(header))
