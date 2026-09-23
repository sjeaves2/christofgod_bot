"""The bot's identity images must live in git.

On 2026-09-16 someone with the leaked token changed the bot's name, avatar and
description. The name and avatar were restored from memory. **The description
could not be**, because BotFather keeps no history and nobody had a copy — it
was reconstructed from what people remembered it saying.

The avatar and description picture were the last two pieces with no copy
anywhere. A file in git is the only form of backup that cannot be edited away
by whoever holds the token.

These tests fail until both images are committed, and fail again if either is
ever removed or replaced with something that is not a real image.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AVATAR = REPO / "deploy" / "botfather-avatar.png"
DESCRIPTION_PIC = REPO / "deploy" / "botfather-description-pic.png"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Small enough to allow a modest export, large enough that an empty file, a
#: git-lfs pointer or a 1x1 placeholder fails.
MIN_BYTES = 5_000


def _png_size(path: Path) -> tuple[int, int]:
    """Width and height from the IHDR chunk, without a third-party library."""
    head = path.read_bytes()[:24]
    assert head[:8] == PNG_MAGIC, f"{path.name} is not a PNG"
    return struct.unpack(">II", head[16:24])


@pytest.mark.parametrize("path", [AVATAR, DESCRIPTION_PIC],
                         ids=lambda p: p.name)
class TestIdentityImagesArePresent:
    def test_the_file_exists(self, path: Path):
        assert path.is_file(), (
            f"{path.relative_to(REPO)} is missing. BotFather keeps no history, "
            "so an image that exists only in Telegram cannot be restored after "
            "an account takeover — which is exactly how the bot's description "
            "was lost on 2026-09-16. See deploy/BOTFATHER.md."
        )

    def test_it_is_a_real_png(self, path: Path):
        assert path.read_bytes()[:8] == PNG_MAGIC, f"{path.name} is not a PNG"

    def test_it_is_not_a_placeholder(self, path: Path):
        size = path.stat().st_size
        assert size >= MIN_BYTES, (
            f"{path.name} is only {size} bytes — too small to be the real "
            "image, so restoring from it would not restore anything"
        )


class TestAvatarIsUsable:
    def test_the_avatar_is_square(self):
        """Telegram crops a profile photo to a circle; a non-square export
        loses the edges of the logo."""
        w, h = _png_size(AVATAR)
        assert w == h, f"avatar is {w}x{h}; Telegram expects a square image"

    def test_the_avatar_is_large_enough_to_upload(self):
        w, _ = _png_size(AVATAR)
        assert w >= 512, f"avatar is only {w}px wide; Telegram wants at least 512"


class TestDocumentationPointsAtThem:
    def test_botfather_md_names_both_files(self):
        doc = (REPO / "deploy" / "BOTFATHER.md").read_text()
        assert "botfather-avatar.png" in doc
        assert "botfather-description-pic.png" in doc
