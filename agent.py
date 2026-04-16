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
import re
import time

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from num2words import num2words

async_client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Detecta cantidades de dinero: $1.234.567, $1,234,567, 1234567.00, etc.
_MONEY_RE = re.compile(
    r'\$\s*([\d.,]+(?:\.\d{1,2})?)'    # $1.234.567 o $1,234,567.50
    r'|([\d]{1,3}(?:[.,]\d{3})+(?:\.\d{1,2})?)'  # 1.234.567 o 1,234,567.50
    r'|([\d]+\.00)',                     # 50000.00
    re.IGNORECASE,
)


def _parse_cop(raw: str) -> int | None:
    """Intenta parsear una cadena numérica a entero COP (sin centavos)."""
    # Normalizar separadores: si el último separador es ',' con 2 decimales → decimal
    # Para COP asumimos siempre enteros; quitamos cualquier .XX al final
    clean = raw.strip()
    # Quitar parte decimal si termina en .XX
    clean = re.sub(r'\.\d{1,2}$', '', clean)
    # Quitar separadores de miles (punto o coma)
    clean = clean.replace('.', '').replace(',', '')
    try:
        return int(clean)
    except ValueError:
        return None


def format_for_tts(text: str) -> str:
    """Convierte cantidades de dinero en el texto a palabras en español (COP).

    Ejemplos:
      "$1.234.567"  →  "un millón doscientos treinta y cuatro mil quinientos sesenta y siete pesos"
      "50000.00"    →  "cincuenta mil pesos"
    """
    def replace_match(m: re.Match) -> str:
        raw = m.group(1) or m.group(2) or m.group(3)
        valor = _parse_cop(raw)
        if valor is None or valor < 0:
            return m.group(0)
        palabras = num2words(valor, lang='es')
        return f"{palabras} pesos"

    resultado = _MONEY_RE.sub(replace_match, text)
    # Eliminar "pesos pesos" si el texto original ya tenía la palabra
    resultado = re.sub(r'\bpesos\s+pesos\b', 'pesos', resultado, flags=re.IGNORECASE)
    return resultado

SYSTEM_PROMPT = """Eres un asistente de voz inteligente para ISERV, empresa colombiana especializada
en infraestructura eléctrica. ISERV se encarga de toda la instalación y gestión
eléctrica de proyectos de vivienda: apartamentos, conjuntos residenciales y demás
edificaciones. No es una constructora general; su core es la parte eléctrica de
cada proyecto. Tienes acceso a su base de datos PostgreSQL.
Responde SIEMPRE en español colombiano. Sé breve y conversacional; tus respuestas
se convierten a voz, así que NUNCA uses markdown, asteriscos, guiones, viñetas,
símbolos ni saltos de línea.
Cuando necesites datos, usa las herramientas disponibles ANTES de responder.
Si una query falla, corrígela. Da solo la información esencial; si hay muchos
resultados, resume los más relevantes.

━━ DOMINIO DE LA EMPRESA ━━
ISERV: empresa de infraestructura eléctrica colombiana. Ejecuta la parte eléctrica
de múltiples proyectos de construcción (apartamentos, conjuntos residenciales).
Sus compras son principalmente materiales eléctricos: cables, acometidas, tableros,
breakers, tuberías conduit, luminarias, tomacorrientes, etc.
Moneda: COP (pesos colombianos). Las fechas están en UTC.
Hoy: usa NOW() para fechas relativas (hoy, este mes, este año).

━━ TABLAS PRINCIPALES ━━

factura (9.218 registros, 2020–2026)
  factura_id, numero, fecha_emision, fecha_vencimiento, moneda,
  proveedor_id → proveedor.proveedor_id,
  cliente_id  → cliente.cliente_id,
  total_subtotal, total_iva, total_retefuente, total_factura,
  orden_compra, pedido, project_id → projects.project_id

factura_detalle (líneas de cada factura)
  detalle_id, factura_id, linea, descripcion, cantidad, unidad,
  precio_unitario, descuento_pct, subtotal, iva_pct, iva_valor, total_linea,
  descripcion_estandarizada, producto_estandarizado

proveedor (207 proveedores)
  proveedor_id, nit, razon_social, telefono, email, ciudad
  Top proveedores: CABLES Y ACCESORIOS ELECTRICOS S.A.S, FRANCISCO MURILLO S.A.S.,
  FERRETERIA TÉCNICA S.A., CABLECOL Y CIA S.C.A., INVERSIONES PRIMERA LIMITADA

cliente (150 clientes)
  cliente_id, nit, razon_social, telefono, email, ciudad

projects (proyectos de construcción)
  project_id, nombre_proyecto
  Proyectos activos: PRIMAVERA, PIAMONTE, LIRIOS, ATLANTIS, BOSKETO, CEIBA,
  JAGGUA, AQUA, ALMERIA, MANZANILLO, VELEROS, CERRO VERDE, LOS BALSOS,
  LAS MARGARITAS, LORIENT, NOGALES, SELVA, KYUX, VIBRA, ZITIZEN, entre otros.

centro_costos  — cc (código), nombre (nombre del centro)
nomina (41.955 registros)
  cedula, nombre, cargo, centro_costos, salario_mes, costo_hora, costo_total,
  horas, fecha, quincena, periodo, project_id
presupuesto
  project_id, codigo, grupo, descripcion, unidad, cantidad, precio
requerimientos (3.135 registros) — solicitudes de materiales por proyecto
  item, descripcion_tecnica, cantidad, unidad, centro_costos, obra_proyecto,
  fecha_solicitud, fecha_requerida, project_id
cotizaciones / cotizaciones_detalle — proceso de compras
inventario — stock de materiales por proyecto
orden_compra — órdenes de compra emitidas
solicitudes_pedidos — pedidos aprobados por Telegram

━━ JOINS MÁS USADOS ━━
Factura + proveedor:  JOIN proveedor p ON f.proveedor_id = p.proveedor_id
Factura + proyecto:   JOIN projects pr ON f.project_id = pr.project_id
Factura + detalle:    JOIN factura_detalle d ON f.factura_id = d.factura_id
Nómina + proyecto:    WHERE project_id = (SELECT project_id FROM projects WHERE nombre_proyecto ILIKE '%nombre%')

━━ BÚSQUEDA DIFUSA — REGLA GENERAL ━━
El usuario habla por teléfono. Nunca pronuncia nombres exactos.
SIEMPRE aplica búsqueda difusa a CUALQUIER texto que mencione, sin importar
de qué tabla o columna se trate: proveedores, proyectos, productos, empleados,
centros de costo, descripciones, ciudades, cargos, conceptos, lo que sea.

La extensión pg_trgm está activa. Patrón universal:

  WHERE similarity(columna_texto, 'lo que dijo el usuario') > 0.2
     OR columna_texto ILIKE '%lo que dijo el usuario%'
  ORDER BY similarity(columna_texto, 'lo que dijo el usuario') DESC

Umbral 0.2 para textos cortos o con acento; puedes bajar a 0.1 si no hay resultados.
Siempre ordena por similarity DESC para que el más parecido quede primero.

INFERENCIA: No preguntes si no estás seguro del nombre. Elige el resultado con
mayor similarity, ejecuta la consulta con ese match y en tu respuesta menciona
cómo interpretaste el nombre. Ejemplo: "Entendí que preguntabas por Cables y
Accesorios Eléctricos. El total es..."
Si realmente hay ambigüedad entre dos opciones muy distintas, pregunta brevemente.

━━ PATRONES FRECUENTES ━━
- "facturas de X proveedor" → JOIN proveedor + similarity/ILIKE en razon_social
- "gastos del proyecto Y"   → JOIN projects + similarity/ILIKE en nombre_proyecto
- "este mes"                → DATE_TRUNC('month', fecha_emision) = DATE_TRUNC('month', NOW())
- "este año"                → EXTRACT(year FROM fecha_emision) = EXTRACT(year FROM NOW())
- totales en COP            → SUM(total_factura) — pesos colombianos, sin centavos
- nómina de un período      → WHERE quincena = '1' AND periodo = 1 AND EXTRACT(year FROM fecha) = 2025"""

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
    """Arranca el servidor MCP de PostgreSQL y registra todas sus herramientas."""
    global _exit_stack, _mcp_tools

    db_url = _build_db_url()
    params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-postgres", db_url],
        env={**os.environ, "NODE_TLS_REJECT_UNAUTHORIZED": "0"},
    )

    print("[MCP] Iniciando servidor PostgreSQL...")
    _exit_stack = contextlib.AsyncExitStack()
    read, write = await _exit_stack.enter_async_context(stdio_client(params))
    session: ClientSession = await _exit_stack.enter_async_context(
        ClientSession(read, write)
    )
    await session.initialize()
    print("[MCP] Sesión inicializada.")

    tools_result = await session.list_tools()
    _mcp_tools = [async_mcp_tool(t, session) for t in tools_result.tools]

    names = [t.name for t in tools_result.tools]
    print(f"[MCP] Herramientas disponibles: {names}")
    print("[MCP] PostgreSQL listo.")


async def close_mcp() -> None:
    """Cierra el proceso MCP al apagar FastAPI."""
    global _exit_stack
    if _exit_stack:
        await _exit_stack.aclose()
        print("[MCP] PostgreSQL desconectado.")


async def get_agent_response(user_text: str, history: list) -> str:
    """Consulta a Claude con las herramientas MCP y retorna la respuesta final."""
    t_start = time.perf_counter()
    print(f"\n{'='*60}")
    print(f"[Agente] Pregunta recibida: {user_text!r}")

    history.append({"role": "user", "content": user_text})

    runner = async_client.beta.messages.tool_runner(
        model="claude-opus-4-6",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        tools=_mcp_tools,
        messages=history,
    )

    final_message = None
    turn = 0
    async for message in runner:
        turn += 1
        t_turn = time.perf_counter() - t_start
        print(f"[Agente] Turno {turn} ({t_turn:.2f}s) — stop_reason: {message.stop_reason}")

        for block in message.content:
            if not hasattr(block, "type"):
                continue
            if block.type == "tool_use":
                sql = str(block.input.get("query", "")).lower()
                print(f"  [tool_use] {block.name}")
                if sql:
                    print(f"  [SQL]      {sql[:120]}")
            elif block.type == "tool_result":
                content_preview = str(getattr(block, "content", ""))[:200]
                print(f"  [tool_result] {content_preview}")
            elif block.type == "text" and block.text:
                print(f"  [texto]    {block.text[:200]}")

        final_message = message

    t_total = time.perf_counter() - t_start
    if final_message:
        history.append({"role": "assistant", "content": final_message.content})
        respuesta = next(
            (b.text for b in final_message.content if b.type == "text"), ""
        )
        respuesta = format_for_tts(respuesta)
        print(f"[Agente] Respuesta final ({t_total:.2f}s): {respuesta[:200]}")
        print(f"{'='*60}\n")
        return respuesta

    print(f"[Agente] Sin respuesta ({t_total:.2f}s)")
    return "Lo siento, no pude procesar tu solicitud."
