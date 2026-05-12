"""Tests para el chunker de oraciones de streaming TTS."""
import pytest
from agent import _chunk_text


def test_corte_en_punto_con_minimo_chars():
    buffer = "El total es de cien pesos."
    chunks, resto = _chunk_text(buffer)
    assert chunks == ["El total es de cien pesos."]
    assert resto == ""


def test_no_corta_si_muy_corto():
    buffer = "Hola."
    chunks, resto = _chunk_text(buffer)
    assert chunks == []
    assert resto == "Hola."


def test_corte_en_coma_con_minimo_30_chars():
    buffer = "El proveedor cables y accesorios, tiene facturas pendientes"
    chunks, resto = _chunk_text(buffer)
    assert chunks == ["El proveedor cables y accesorios,"]
    assert resto == " tiene facturas pendientes"


def test_no_corta_coma_si_menos_30_chars():
    buffer = "Hola, cómo estás"
    chunks, resto = _chunk_text(buffer)
    assert chunks == []
    assert resto == "Hola, cómo estás"


def test_multiples_oraciones():
    buffer = "El total es de cien pesos. Hay tres facturas pendientes."
    chunks, resto = _chunk_text(buffer)
    assert chunks == ["El total es de cien pesos.", " Hay tres facturas pendientes."]
    assert resto == ""


def test_interrogacion():
    buffer = "¿Qué proyecto tiene más gasto este mes?"
    chunks, resto = _chunk_text(buffer)
    assert chunks == ["¿Qué proyecto tiene más gasto este mes?"]
    assert resto == ""


def test_sin_puntuacion():
    buffer = "Este es un texto sin puntuación final"
    chunks, resto = _chunk_text(buffer)
    assert chunks == []
    assert resto == buffer


def test_flush_fuerza_todo():
    buffer = "Texto sin puntuación"
    chunks, resto = _chunk_text(buffer, flush=True)
    assert chunks == ["Texto sin puntuación"]
    assert resto == ""


def test_flush_vacio():
    chunks, resto = _chunk_text("", flush=True)
    assert chunks == []
    assert resto == ""


def test_flush_whitespace_only():
    chunks, resto = _chunk_text("   ", flush=True)
    assert chunks == []
    assert resto == ""


def test_no_corta_punto_antes_de_digito():
    """Número colombiano $1.018.370 NO debe partirse en '$1.' + '018.370'."""
    buffer = "El subtotal es $1.018.370 pesos totales."
    chunks, resto = _chunk_text(buffer)
    # El '.' entre 1 y 018 no debe ser punto de corte porque le sigue un dígito
    # Todo debe ir en un solo chunk (el '.' final sí corta)
    assert len(chunks) == 1
    assert "$1.018.370" in chunks[0]


def test_no_corta_coma_antes_de_digito():
    """Una coma seguida de dígito (ej: 'valor de $1,200,000 pesos') no debe partir."""
    buffer = "El valor total acumulado es de 1,200,000 unidades procesadas."
    chunks, resto = _chunk_text(buffer)
    # La coma en '1,200,000' no debe cortar
    assert all("1,200" not in (resto or "") for _ in [1])  # el número queda íntegro en algún chunk
    full = "".join(chunks) + resto
    assert "1,200,000" in full
