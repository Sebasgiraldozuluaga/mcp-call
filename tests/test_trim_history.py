from main import _trim_history
import copy


def _make_history(n_turns: int) -> list:
    """Creates n_turns user+assistant pairs, each assistant turn with a tool_result."""
    history = []
    for i in range(n_turns):
        history.append({"role": "user", "content": f"pregunta {i}"})
        history.append({
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"respuesta {i}"},
                {
                    "type": "tool_result",
                    "content": "x" * 1000  # long raw SQL output
                }
            ]
        })
    return history


def test_short_history_unchanged():
    h = _make_history(4)  # 8 messages, under 16-message window
    result = _trim_history(h)
    assert len(result) == 8


def test_long_history_trimmed_to_16():
    h = _make_history(10)  # 20 messages
    result = _trim_history(h)
    assert len(result) == 16


def test_original_not_mutated():
    h = _make_history(10)
    original_len = len(h)
    _trim_history(h)
    assert len(h) == original_len


def test_tool_result_str_truncated():
    h = _make_history(2)
    result = _trim_history(h)
    for msg in result:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if block.get("type") == "tool_result" and isinstance(block.get("content"), str):
                    assert len(block["content"]) <= 403  # 400 chars + "… [truncado]"


def test_tool_result_short_not_truncated():
    h = [
        {"role": "user", "content": "hola"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_result", "content": "short"}
            ]
        }
    ]
    result = _trim_history(h)
    assert result[1]["content"][0]["content"] == "short"


def test_first_message_is_most_recent_when_trimmed():
    h = _make_history(10)  # 20 messages: turns 0-9
    result = _trim_history(h)
    # Should keep last 8 turns (16 messages) → turns 2-9 kept, turns 0-1 dropped
    assert result[0]["content"] == "pregunta 2"


def test_trim_history_handles_pydantic_blocks():
    """_trim_history must not crash when assistant content blocks are Pydantic objects (ParsedBetaTextBlock)."""
    from anthropic.types import TextBlock

    pydantic_block = TextBlock(type="text", text="respuesta bonita")
    h = [
        {"role": "user", "content": "pregunta"},
        {"role": "assistant", "content": [pydantic_block]},
    ]
    # Should NOT raise AttributeError: 'TextBlock' object has no attribute 'get'
    result = _trim_history(h)
    assert len(result) == 2
