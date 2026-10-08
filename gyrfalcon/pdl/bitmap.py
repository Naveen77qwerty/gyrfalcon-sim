"""128-bit receive bitmap helpers (paper section 4.1)."""

BITMAP_BITS = 128


def set_bit(bitmap: int, offset: int) -> int:
    if offset < 0 or offset >= BITMAP_BITS:
        return bitmap
    return bitmap | (1 << offset)


def get_bit(bitmap: int, offset: int) -> bool:
    if offset < 0 or offset >= BITMAP_BITS:
        return False
    return bool(bitmap & (1 << offset))


def slide_while_received(base: int, bitmap: int) -> tuple[int, int]:
    """Advance base while the lowest bit is set."""
    while get_bit(bitmap, 0):
        bitmap >>= 1
        base += 1
    return base, bitmap
