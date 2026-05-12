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
