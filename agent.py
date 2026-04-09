"""
Agente Claude conectado al servidor MCP oficial de PostgreSQL.

Flujo:
  1. init_mcp()  →  arranca npx @modelcontextprotocol/server-postgres via stdio
  2. Claude recibe las herramientas del MCP (query, list-tables, describe-table…)
  3. get_agent_response()  →  loop tool_runner hasta end_turn
  4. close_mcp()  →  cierra el proceso MCP al apagar el servidor
"""
import contextlib
import os

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

async_client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """Eres un asistente de voz inteligente con acceso a una base de datos PostgreSQL.
Responde SIEMPRE en español. Sé breve y conversacional; tus respuestas serán convertidas a voz.
No uses markdown, asteriscos, guiones, viñetas ni símbolos especiales.
Cuando necesites datos, usa las herramientas disponibles antes de responder.
Si una query falla, intenta corregirla. Limita las respuestas a lo esencial."""

# Estado global del MCP (se inicializa una vez al arrancar el servidor)
_exit_stack: contextlib.AsyncExitStack | None = None
_mcp_tools: list = []


def _build_db_url() -> str:
    """Construye la URL de PostgreSQL asegurando sslmode=require."""
    url = os.environ["DATABASE_URL"]
    if "sslmode" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


async def init_mcp() -> None:
    """Arranca el servidor MCP de PostgreSQL y registra sus herramientas.

    Llama esta función UNA VEZ en el lifespan de FastAPI.
    """
    global _exit_stack, _mcp_tools

    db_url = _build_db_url()
    params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-postgres", db_url],
        env={**os.environ, "NODE_TLS_REJECT_UNAUTHORIZED": "0"},
    )

    _exit_stack = contextlib.AsyncExitStack()
    read, write = await _exit_stack.enter_async_context(stdio_client(params))
    session: ClientSession = await _exit_stack.enter_async_context(
        ClientSession(read, write)
    )
    await session.initialize()

    tools_result = await session.list_tools()
    _mcp_tools = [async_mcp_tool(t, session) for t in tools_result.tools]

    names = [t.name for t in tools_result.tools]
    print(f"[MCP] PostgreSQL listo. Herramientas disponibles: {names}")


async def close_mcp() -> None:
    """Cierra el proceso MCP al apagar FastAPI."""
    global _exit_stack
    if _exit_stack:
        await _exit_stack.aclose()
        print("[MCP] PostgreSQL desconectado.")


async def get_agent_response(user_text: str, history: list) -> str:
    """Consulta a Claude con las herramientas MCP y retorna la respuesta final."""
    history.append({"role": "user", "content": user_text})

    runner = async_client.beta.messages.tool_runner(
        model="claude-opus-4-6",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        tools=_mcp_tools,
        messages=history,
    )

    final_message = None
    async for message in runner:
        # Loguear llamadas a herramientas para debug
        for block in message.content:
            if hasattr(block, "type") and block.type == "tool_use":
                print(f"  [tool] {block.name}({block.input})")
        final_message = message

    if final_message:
        history.append({"role": "assistant", "content": final_message.content})
        return next(
            (b.text for b in final_message.content if b.type == "text"), ""
        )

    return "Lo siento, no pude procesar tu solicitud."
