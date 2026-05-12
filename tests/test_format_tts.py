import pytest
from agent import _parse_number, format_for_tts


# --- _parse_number ---

def test_parse_colombian_millions():
    """1.234.567 uses dots as thousands separators."""
    assert _parse_number("1.234.567") == 1_234_567

def test_parse_colombian_thousands():
    assert _parse_number("50.000") == 50_000

def test_parse_plain_integer():
    assert _parse_number("1234567") == 1_234_567

def test_parse_comma_thousands():
    """Some systems use comma as thousands separator."""
    assert _parse_number("1,234,567") == 1_234_567

def test_parse_decimal_ignored():
    """Trailing decimal cents should be stripped."""
    assert _parse_number("1234,56") == 1234

def test_parse_dot_decimal_ignored():
    assert _parse_number("1234.56") == 1234

def test_parse_small():
    assert _parse_number("500") == 500


# --- format_for_tts with approximation ---

def test_millions_contains_millon():
    result = format_for_tts("$1.234.567")
    assert "millón" in result or "millones" in result, f"Got: {result}"

def test_millions_contains_pesos():
    result = format_for_tts("$1.234.567")
    assert "pesos" in result, f"Got: {result}"

def test_millions_not_un_peso():
    result = format_for_tts("$1.234.567")
    assert result.strip() != "un peso", f"Got: {result}"

def test_exact_million():
    result = format_for_tts("$1.000.000")
    assert "millón" in result or "un millón" in result, f"Got: {result}"
    assert "pesos" in result

def test_thousands_range():
    result = format_for_tts("$45.678")
    assert "cuarenta" in result or "cuarenta y seis" in result, f"Got: {result}"
    assert "pesos" in result

def test_small_exact():
    result = format_for_tts("$4.500")
    assert "cuatro mil quinientos pesos" == result, f"Got: {result}"

def test_no_dollar_number():
    result = format_for_tts("67 facturas")
    assert "sesenta y siete facturas" == result, f"Got: {result}"
