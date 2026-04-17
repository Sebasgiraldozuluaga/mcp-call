# MCP-Call

Agente de voz para llamadas telefónicas con Claude + Twilio + ElevenLabs. El bot de Telegram permite iniciar llamadas y recibe un resumen de costo al terminar.

## Arquitectura

- **FastAPI** — servidor HTTP/WebSocket
- **Twilio Media Streams** — audio de la llamada en tiempo real (μ-law 8 kHz)
- **ElevenLabs Scribe v1** — STT (transcripción)
- **Claude Sonnet 4.6 + MCP** — agente con acceso a base de datos PostgreSQL de ISERV
- **ElevenLabs Turbo v2.5** — TTS (respuesta de voz)
- **Telegram Bot** — disparador de llamadas + resumen de costo al finalizar

## Estructura

- `main.py` — servidor FastAPI, pipeline de audio WebSocket, bot de Telegram
- `agent.py` — agente Claude con herramientas MCP
- `audio_utils.py` — utilidades μ-law, WAV, RMS
- `mcp_servers.json` — configuración de servidores MCP (ej. PostgreSQL)
- `requirements.txt` — dependencias

## Variables de entorno (.env)

```env
# Twilio
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxx
TWILIO_PHONE_NUMBER=+1xxxxxxxxxx     # Número Twilio que hace la llamada

# ElevenLabs
ELEVENLABS_API_KEY=sk_xxxxxxxxxxxxxxxx
ELEVENLABS_VOICE_ID=xxxxxxxxxxxxxxxx  # ID de la voz a usar

# Anthropic
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx

# Telegram
TELEGRAM_BOT_TOKEN=xxxxxxxxxx:xxxxxxxxxxxxxxxxxxxxxxxxxxx  # Token del BotFather
TELEGRAM_ALLOWED_ID=123456789        # Tu Telegram user ID (solo este puede usar /call)

# Servidor
SERVER_URL=https://tu-dominio.ngrok.io  # URL pública con HTTPS (Twilio la necesita)

# Teléfono destino por defecto (opcional)
YOUR_PHONE_NUMBER=+573001234567

# Base de datos (usado por mcp_servers.json)
DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

## Instalación

```bash
git clone <URL-del-repositorio>
cd mcp-call
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edita con tus credenciales
```

## Correrlo

```bash
source .venv/bin/activate
python main.py
```

El servidor queda en `http://0.0.0.0:8000`. Necesitas exponerlo públicamente (ngrok, tunnel, etc.) y poner esa URL en `SERVER_URL`.

```bash
# Ejemplo con ngrok
ngrok http 8000
# Copia la URL https://xxx.ngrok.io → ponla en SERVER_URL del .env
```

## Uso desde Telegram

1. Obtén tu `TELEGRAM_ALLOWED_ID`: habla con [@userinfobot](https://t.me/userinfobot) en Telegram.
2. Crea tu bot con [@BotFather](https://t.me/BotFather) y copia el token a `TELEGRAM_BOT_TOKEN`.
3. Levanta el servidor. El bot arranca en modo **polling** automáticamente.

### Comandos disponibles

| Comando | Descripción |
|---------|-------------|
| `/call +573001234567` | Llama al número indicado |
| `/call` | Llama al número en `YOUR_PHONE_NUMBER` |

Al terminar la llamada, el bot envía automáticamente un resumen con el costo estimado (Claude, ElevenLabs STT/TTS, Twilio).

### Ejemplo de flujo

```
Tú → /call +573001234567
Bot → "Llamando a +573001234567... (CAxxxxxxxx)"
[suena el teléfono]
[conversación con el agente de voz]
[cuelgas]
Bot → 📞 Llamada finalizada — +573001234567
      🤖 Claude Sonnet 4.6
        • Input: 1,234 tokens (~$0.0037)
        • Output: 89 tokens (~$0.0013)
      🎤 ElevenLabs STT: 45.2s (~$0.0050)
      🔊 ElevenLabs TTS: 320 chars (~$0.0480)
      📱 Twilio: 62s → 2 min (~$0.0960)
      💰 Costo total estimado: ~$0.1540 USD
```

## Configuración MCP (`mcp_servers.json`)

```json
{
  "servers": {
    "postgres": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-postgres"],
      "append_env_var": "DATABASE_URL",
      "ssl_mode": "require"
    }
  }
}
```

## Requisitos

- Python 3.12+
- Node.js (para servidores MCP via `npx`)
- ffmpeg (usado por pydub para el fallback gTTS)
- Cuenta Twilio con número habilitado para llamadas salientes
- Cuenta ElevenLabs con créditos STT + TTS
