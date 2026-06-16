"""Tests for pdf_generator.py — user list PDF generation."""

import sys
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from pdf_generator import generate_user_list_pdf


def _users(n: int) -> list[dict]:
    return [
        {
            "chat_id": i,
            "username": f"user{i}",
            "display_name": f"User Number {i}",
            "joined": "2025-01-01",
        }
        for i in range(1, n + 1)
    ]


class TestGenerateUserListPdf:
    def test_returns_bytesio(self):
        result = generate_user_list_pdf(_users(5))
        assert isinstance(result, BytesIO)

    def test_output_is_nonempty(self):
        result = generate_user_list_pdf(_users(5))
        assert result.tell() > 0 or len(result.read()) > 0

    def test_output_starts_with_pdf_magic_bytes(self):
        buf = generate_user_list_pdf(_users(3))
        buf.seek(0)
        assert buf.read(4) == b"%PDF"

    def test_empty_list_still_produces_pdf(self):
        buf = generate_user_list_pdf([])
        buf.seek(0)
        assert buf.read(4) == b"%PDF"

    def test_large_list_produces_pdf(self):
        buf = generate_user_list_pdf(_users(150))
        buf.seek(0)
        assert buf.read(4) == b"%PDF"

    def test_user_without_username_handled(self):
        users = [{"chat_id": 1, "display_name": "No Username", "joined": "2025-01-01"}]
        buf = generate_user_list_pdf(users)
        buf.seek(0)
        assert buf.read(4) == b"%PDF"

    def test_user_without_display_name_handled(self):
        users = [{"chat_id": 1, "username": "nondisplay", "joined": "2025-01-01"}]
        buf = generate_user_list_pdf(users)
        buf.seek(0)
        assert buf.read(4) == b"%PDF"

    def test_buffer_is_rewound_to_start(self):
        buf = generate_user_list_pdf(_users(2))
        assert buf.tell() == 0
