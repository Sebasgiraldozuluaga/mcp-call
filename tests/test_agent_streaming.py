"""Tests para get_agent_response_streaming con mocks de Anthropic."""
import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Set dummy key before importing agent
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")


@pytest.mark.asyncio
async def test_streaming_sin_tools_produce_chunks():
    """Respuesta simple sin tool calls produce chunks y sentinel None al final."""
    from agent import get_agent_response_streaming, _TOOL_USE_SENTINEL

    texto_respuesta = "El total de facturas es de cien mil pesos. Hay tres proveedores activos."

    # Collect what gets put in the queue
    items_recibidos = []

    class FakeQueue:
        async def put(self, item):
            items_recibidos.append(item)

    # Build mock stream context manager
    mock_delta = MagicMock()
    mock_delta.type = "text_delta"
    mock_delta.text = texto_respuesta

    mock_event = MagicMock()
    mock_event.type = "content_block_delta"
    mock_event.delta = mock_delta

    mock_usage = MagicMock()
    mock_usage.input_tokens = 100
    mock_usage.output_tokens = 20

    mock_content_block = MagicMock()
    mock_content_block.type = "text"
    mock_content_block.text = texto_respuesta

    mock_final_msg = MagicMock()
    mock_final_msg.usage = mock_usage
    mock_final_msg.stop_reason = "end_turn"
    mock_final_msg.content = [mock_content_block]

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield mock_event

        async def get_final_message(self):
            return mock_final_msg

    with patch("agent.async_client") as mock_client:
        mock_client.beta.messages.stream.return_value = FakeStream()
        history = []
        in_tok, out_tok, assistant_content = await get_agent_response_streaming(
            "cuántas facturas hay", history, FakeQueue()
        )

    assert in_tok == 100
    assert out_tok == 20
    assert items_recibidos[-1] is None, "Último item debe ser None (sentinel de fin)"
    # Al menos un chunk de texto antes del sentinel
    text_items = [i for i in items_recibidos if isinstance(i, str) and i != _TOOL_USE_SENTINEL]
    assert len(text_items) >= 1
    # El historial dentro de la función solo tiene el user (assistant lo agrega main.py)
    assert len(history) == 1  # solo user
    assert assistant_content is not None


@pytest.mark.asyncio
async def test_sentinel_none_siempre_se_envia_en_error():
    """Incluso si hay excepción, None se envía al final."""
    from agent import get_agent_response_streaming

    items_recibidos = []

    class FakeQueue:
        async def put(self, item):
            items_recibidos.append(item)

    class BrokenStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            raise RuntimeError("conexión perdida")
            yield  # make it an async generator

        async def get_final_message(self):
            return MagicMock()

    with patch("agent.async_client") as mock_client:
        mock_client.beta.messages.stream.return_value = BrokenStream()
        history = []
        in_tok, out_tok, assistant_content = await get_agent_response_streaming(
            "pregunta que falla", history, FakeQueue()
        )

    assert items_recibidos[-1] is None, "None debe enviarse incluso en error"
