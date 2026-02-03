"""
Unit tests for password hashing utilities.

Run with: pytest test_security.py -v
"""
import pytest

from app.security import hash_password, verify_password


def test_hash_password_returns_hash():
    password = "correct horse battery staple"
    password_hash = hash_password(password)

    assert isinstance(password_hash, str)
    assert password_hash != password


def test_hash_password_is_salted():
    password = "same-password"
    first_hash = hash_password(password)
    second_hash = hash_password(password)

    assert first_hash != second_hash


def test_hash_password_rejects_empty():
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_password_success():
    password = "hunter2"
    password_hash = hash_password(password)

    assert verify_password(password, password_hash) is True


def test_verify_password_failure():
    password_hash = hash_password("right-password")

    assert verify_password("wrong-password", password_hash) is False


def test_verify_password_handles_empty():
    assert verify_password("", "not-empty") is False
    assert verify_password("not-empty", "") is False
