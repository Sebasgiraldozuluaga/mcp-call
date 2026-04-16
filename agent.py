"""
Agente Claude conectado a servidores MCP configurados via JSON.

Flujo:
  1. init_mcp()  →  lee mcp_servers.json y arranca cada servidor via stdio
  2. Claude recibe las herramientas de todos los MCPs
  3. get_agent_response()  →  loop tool_runner hasta end_turn
  4. close_mcp()  →  cierra todos los procesos MCP al apagar el servidor
"""
import contextlib
import json
import os
import re
import time
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from num2words import num2words

async_client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Paso 1: dinero con $ → "X pesos"
_MONEY_RE = re.compile(r'\$\s*([\d.,]+)')
# Paso 2: cualquier número restante → palabras (sin "pesos")
_NUMBER_RE = re.compile(r'[\d]{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|[\d]+')


def _parse_number(raw: str) -> int | None:
    """Parsea una cadena numérica a entero, quitando separadores de miles."""
    clean = raw.strip()
    # Quitar decimales finales (.XX o ,XX)
    clean = re.sub(r'[.,]\d{1,2}$', '', clean)
    clean = clean.replace('.', '').replace(',', '')
    try:
        return int(clean)
    except ValueError:
        return None


def format_for_tts(text: str) -> str:
    """Convierte todos los números del texto a palabras en español para TTS.

    - Números con $  → "doscientos mil pesos"
    - Números sin $  → "doscientos mil" (sin pesos)

    Ejemplos:
      "$1.234.567"     → "un millón doscientos treinta y cuatro mil quinientos sesenta y siete pesos"
      "200.390 metros" → "doscientos mil trescientos noventa metros"
      "67 facturas"    → "sesenta y siete facturas"
    """
    def replace_money(m: re.Match) -> str:
        valor = _parse_number(m.group(1))
        if valor is None or valor < 0:
            return m.group(0)
        return f"{num2words(valor, lang='es')} pesos"

    def replace_number(m: re.Match) -> str:
        valor = _parse_number(m.group(0))
        if valor is None or valor < 0:
            return m.group(0)
        return num2words(valor, lang='es')

    resultado = _MONEY_RE.sub(replace_money, text)
    resultado = re.sub(r'\bpesos\s+pesos\b', 'pesos', resultado, flags=re.IGNORECASE)
    resultado = _NUMBER_RE.sub(replace_number, resultado)
    resultado = resultado.replace('%', ' por ciento')
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

flujo_productos (4.908 registros, 2026) — MOVIMIENTOS DE MATERIAL EN OBRA
  id, producto, cantidad, unidad, db_type, sent_date, metadata_id, project_id
  db_type: "Salida de Material" (4.898) | "Ingreso de Material" (10)
  sent_date: timestamp con timezone (usar para filtros de fecha)
  ⚠ IMPORTANTE:
    - "Gasto de material" / "consumo" / "salidas" = flujo_productos WHERE db_type = 'Salida de Material'
    - "Ingreso de material" / "entradas a obra" = flujo_productos WHERE db_type = 'Ingreso de Material'
    - "Compras de material" / "facturas" = tabla factura + factura_detalle (tiene precios)
    - flujo_productos NO tiene precios, solo cantidades físicas (metros, unidades, etc.)

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
Flujo + proyecto:     JOIN projects pr ON fp.project_id = pr.project_id
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
- "compras del proyecto Y"  → factura + factura_detalle JOIN projects (tiene precios $)
- "gasto/consumo de material del proyecto Y" → flujo_productos WHERE db_type='Salida de Material' JOIN projects
- "ingreso de material"     → flujo_productos WHERE db_type='Ingreso de Material'
- "qué material se gastó esta semana en Y" → flujo_productos + projects, filtrar por sent_date y nombre_proyecto
- "este mes"                → DATE_TRUNC('month', sent_date) = DATE_TRUNC('month', NOW())
- "este año"                → EXTRACT(year FROM sent_date) = EXTRACT(year FROM NOW())
- "esta semana"             → sent_date >= DATE_TRUNC('week', NOW())
- totales en COP            → SUM(total_factura) — pesos colombianos, sin centavos
- nómina de un período      → WHERE quincena = '1' AND periodo = 1 AND EXTRACT(year FROM fecha) = 2025

━━ ANÁLISIS INTELIGENTE ━━
Cuando el usuario pregunte por gasto de material, además de dar los números,
ofrece un análisis breve y útil. Por ejemplo:
- Si hay mucho cable saliendo → probablemente están en fase de cableado/alambrado
- Si hay muchos breakers y tableros → están en fase de montaje de tableros
- Si hay tomacorrientes y suiches → están en fase de aparatos/acabados
- Compara con períodos anteriores si es relevante
- Menciona los materiales más consumidos y qué fase del proyecto sugieren

━━ FORMATO DE RESPUESTA PARA VOZ ━━
Tus respuestas se convierten a voz (TTS). Reglas estrictas:
- Dinero SIEMPRE con símbolo $: "$1.234.567", "$50.000". NUNCA escribas "1.234.567 pesos".
- Cantidades que NO son dinero (metros, unidades, rollos, horas) escríbelas con número
  sin separador de miles: "200390 metros", "1400 unidades", "67 facturas".
- NUNCA uses puntos como separador de miles en cantidades que no son dinero.
- Números menores a 10.000 déjalos como dígitos: "600 metros", "30 metros".
- Porcentajes en palabras: "quince por ciento" en vez de "15%"."""

# Estado global del MCP (se inicializa una vez al arrancar el servidor)
_exit_stack: contextlib.AsyncExitStack | None = None
_mcp_tools: list = []

# Ruta al archivo de configuración (junto a este script)
_CONFIG_PATH = Path(__file__).parent / "mcp_servers.json"


def _load_server_configs() -> dict:
    """Lee y retorna la configuración de servidores MCP desde JSON."""
    with open(_CONFIG_PATH) as f:
        config = json.load(f)
    return config.get("servers", {})


def _build_server_params(name: str, cfg: dict) -> StdioServerParameters:
    """Construye StdioServerParameters a partir de la config JSON de un servidor."""
    # Construir args: copiar los args base del JSON
    args = list(cfg.get("args", []))

    # Si el servidor necesita una variable de entorno como argumento final
    # (ej: DATABASE_URL para postgres), la resuelve y la agrega a los args
    append_env_var = cfg.get("append_env_var")
    if append_env_var:
        value = os.environ[append_env_var]
        # Opción especial para PostgreSQL: asegurar sslmode
        if cfg.get("ssl_mode") and "sslmode" not in value:
            value += ("&" if "?" in value else "?") + f"sslmode={cfg['ssl_mode']}"
        args.append(value)

    # Merge de variables de entorno: hereda el entorno actual + las del JSON
    env = {**os.environ, **cfg.get("env", {})}

    return StdioServerParameters(
        command=cfg["command"],
        args=args,
        env=env,
    )


async def init_mcp() -> None:
    """Lee mcp_servers.json y arranca todos los servidores MCP configurados."""
    global _exit_stack, _mcp_tools

    server_configs = _load_server_configs()
    if not server_configs:
        print("[MCP] No hay servidores configurados en mcp_servers.json")
        return

    _exit_stack = contextlib.AsyncExitStack()
    _mcp_tools = []

    for name, cfg in server_configs.items():
        print(f"[MCP] Iniciando servidor '{name}'...")
        params = _build_server_params(name, cfg)

        read, write = await _exit_stack.enter_async_context(stdio_client(params))
        session: ClientSession = await _exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await session.initialize()

        tools_result = await session.list_tools()
        server_tools = [async_mcp_tool(t, session) for t in tools_result.tools]
        _mcp_tools.extend(server_tools)

        tool_names = [t.name for t in tools_result.tools]
        print(f"[MCP] '{name}' listo — herramientas: {tool_names}")

    print(f"[MCP] Total herramientas cargadas: {len(_mcp_tools)}")


async def close_mcp() -> None:
    """Cierra todos los procesos MCP al apagar FastAPI."""
    global _exit_stack
    if _exit_stack:
        await _exit_stack.aclose()
        print("[MCP] Todos los servidores desconectados.")


async def get_agent_response(user_text: str, history: list) -> tuple[str, int, int]:
    """Consulta a Claude con las herramientas MCP y retorna (respuesta, input_tokens, output_tokens)."""
    t_start = time.perf_counter()
    print(f"\n{'='*60}")
    print(f"[Agente] Pregunta recibida: {user_text!r}")

    history.append({"role": "user", "content": user_text})

    runner = async_client.beta.messages.tool_runner(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        tools=_mcp_tools,
        messages=history,
    )

    final_message = None
    turn = 0
    total_input_tokens = 0
    total_output_tokens = 0
    async for message in runner:
        turn += 1
        t_turn = time.perf_counter() - t_start
        print(f"[Agente] Turno {turn} ({t_turn:.2f}s) — stop_reason: {message.stop_reason}")

        if hasattr(message, "usage") and message.usage:
            total_input_tokens += getattr(message.usage, "input_tokens", 0)
            total_output_tokens += getattr(message.usage, "output_tokens", 0)

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
        print(f"  [Tokens] input={total_input_tokens} output={total_output_tokens}")
        print(f"{'='*60}\n")
        return respuesta, total_input_tokens, total_output_tokens

    print(f"[Agente] Sin respuesta ({t_total:.2f}s)")
    return "Lo siento, no pude procesar tu solicitud.", total_input_tokens, total_output_tokens
