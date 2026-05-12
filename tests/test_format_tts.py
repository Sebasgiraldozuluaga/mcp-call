import pytest
from agent import _parse_number, _approx_for_tts, format_for_tts


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
    assert "mil" in result, f"Expected thousands in result, got: {result}"
    assert "cuarenta" in result, f"Got: {result}"
    assert "pesos" in result

def test_small_exact():
    result = format_for_tts("$4.500")
    assert "cuatro mil quinientos pesos" == result, f"Got: {result}"

def test_no_dollar_number():
    result = format_for_tts("67 facturas")
    assert "sesenta y siete facturas" == result, f"Got: {result}"


# --- _approx_for_tts direct tests ---

def test_approx_exact_million():
    assert _approx_for_tts(1_000_000) == "un millón"

def test_approx_million_with_remainder():
    result = _approx_for_tts(1_234_567)
    assert "millón" in result
    assert "doscientos" in result  # 200k remainder

def test_approx_999999_stays_below_million():
    result = _approx_for_tts(999_999)
    assert "millón" not in result, f"999999 should not round up to millón, got: {result}"
    assert "mil" in result

def test_approx_thousands():
    result = _approx_for_tts(45_678)
    assert "cuarenta" in result
    assert "mil" in result

def test_approx_small_exact():
    assert _approx_for_tts(4_500) == "cuatro mil quinientos"
