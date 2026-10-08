# 🤖 AgyAgent: Agente de IA Autónomo con Antigravity (`agy`)

**AgyAgent** es un asistente de inteligencia artificial personal, autónomo y persistente (*always-on*) que se ejecuta en tu propia máquina Linux. 

Te permite controlar tu equipo, ejecutar tareas en la terminal, investigar en internet y recibir alertas del sistema directamente a través de **Telegram** o desde la **consola local**, utilizando como cerebro el motor nativo de **Google Antigravity CLI (`agy`)**.

---

## 🧠 ¿Cómo funciona por dentro? (Arquitectura)

A diferencia de los chatbots tradicionales que solo responden texto, AgyAgent funciona como un **servicio continuo (daemon)** que une la mensajería instantánea con el poder de ejecución en tu sistema operativo:

```text
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                            CANALES DE INTERACCIÓN                           │
 │                                                                             │
 │   📱 Telegram (@tu_bot)             💻 Consola Local ("👤 Tú (Consola) >")  │
 └──────────────────────┬──────────────────────────────────┬───────────────────┘
                        │                                  │
                        ▼                                  ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                         DEMONIO PYTHON (bot.py)                             │
 │                                                                             │
 │  • Gestiona la conexión segura con Telegram (HTTPX con auto-reintento)      │
 │  • Modo Dual: permite escribir en terminal y Telegram al mismo tiempo       │
 │  • Heartbeat Loop: despierta cada 15 min para vigilar tareas y alertas      │
 │  • Filtro de seguridad: solo responde a tu Telegram User ID autorizado      │
 └──────────────────────────────────────┬──────────────────────────────────────┘
                                        │ Ejecuta en segundo plano:
                                        │ agy -c -p "<orden>" --dangerously-skip-permissions
                                        ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                      MOTOR NATIVO DE ANTIGRAVITY (agy)                      │
 │                                                                             │
 │  ✅ Autenticación: Usa tu cuenta de Google activa (0 coste de API Keys)     │
 │  ✅ Directrices: Carga automáticamente la personalidad de AGENTS.md        │
 │  ✅ Herramientas: Ejecuta comandos Bash, busca en la web y gestiona ficheros│
 │  ✅ Memoria: Mantiene el hilo conversacional (-c / --continue)             │
 └─────────────────────────────────────────────────────────────────────────────┘
```

---

## ⭐ Ventajas Principales

1. **Cero Coste de API Key:**  
   Al apoyarse en el binario `agy`, el agente utiliza la sesión y cuota de tu cuenta de Google ya vinculada a Antigravity en tu máquina. No necesitas pagar tokens por llamadas a APIs externas.
2. **Ciclo de Heartbeat Activo (Always-On):**  
   El demonio no se queda inactivo esperando a que le hables. Cada 15 minutos (configurable), lanza un pulso silencioso al agente para revisar el estado del sistema, tareas pendientes o errores. Si todo está correcto, guarda silencio (`SILENT_OK`); si detecta algo importante que debas saber, **te envía una notificación proactiva a Telegram**.
3. **Control Dual Simultáneo:**  
   Si estás fuera de casa o en el móvil, le hablas por **Telegram**. Si estás sentado frente al ordenador, puedes escribirle directamente en la **terminal** en la que corre el bot. Ambos canales comparten la misma memoria e historial.
4. **Capacidades Autónomas Reales:**  
   No solo te dice cómo hacer las cosas: las hace. Puede clonar repositorios, compilar código, comprobar el consumo de CPU/RAM, inspeccionar logs del sistema y descargar o analizar ficheros.

---

## 📁 Estructura del Proyecto

```text
agente-agy/
├── bot.py             # Demonio principal: Telegram + Consola Dual + Heartbeat + agy
├── AGENTS.md          # Personalidad, directrices y normas de seguridad de AgyAgent
├── .env               # Variables locales privadas (tokens e IDs) [IGNORADO POR GIT]
├── .env.example       # Plantilla limpia de variables para nuevos despliegues
├── requirements.txt   # Dependencias mínimas de Python (python-telegram-bot, dotenv)
└── README.md          # Esta documentación
```

---

## 🚀 Guía de Instalación y Puesta en Marcha

### 1. Requisitos Previos
* Sistema operativo **Linux** (Ubuntu, Debian o derivado).
* **Python 3.10+** instalado.
* **Antigravity CLI (`agy`)** instalado y autenticado en tu cuenta de Google (`which agy`).

### 2. Configurar el entorno virtual
En la carpeta del proyecto, crea y activa el entorno virtual de Python:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configurar tus credenciales en `.env`
Copia la plantilla `.env.example` a `.env` (si aún no existe) y ajusta tus variables:

```bash
cp .env.example .env
```

Edita el archivo `.env`:
```env
# 1. Token de tu bot obtenido con @BotFather en Telegram
TELEGRAM_BOT_TOKEN=tu_token_aqui

# 2. Tu Telegram User ID numérico (para que SOLO TÚ puedas darle órdenes)
TELEGRAM_ALLOWED_USER_ID=0

# 3. Configuración del Heartbeat
ENABLE_HEARTBEAT=true
HEARTBEAT_MINUTES=15
```

> 💡 **¿Cómo saber tu Telegram User ID?**  
> Si dejas `TELEGRAM_ALLOWED_USER_ID=0`, arranca el bot y escríbele `/start` o `/id` en Telegram. Te dirá tu número exacto. Cópialo en tu `.env` para bloquear el acceso a cualquier otra persona.

---

## 💻 Ejecución y Uso

### Arrancar en Primer Plano (Recomendado para interactuar)
```bash
source .venv/bin/activate
python bot.py
```

Al arrancar verás:
```text
=================================================================
🤖 AgyAgent - Motor agy (Antigravity CLI)
• Autenticación: Sesión activa de Google / Antigravity (Cero API Key)
• Binario agy: ~/.local/bin/agy
• Heartbeat: Activo cada 15 min
• Escuchando en Telegram y en la Consola simultáneamente.
=================================================================
❤️ Heartbeat iniciado (intervalo: cada 15 minutos).

💬 Modo Consola Activo: Puedes escribir aquí tus órdenes directamente en cualquier momento.
👤 Tú (Consola) > 
```

A partir de este momento puedes:
* **Escribirle en la terminal:** Escribe directamente tras `👤 Tú (Consola) >` cualquier orden y presiona Enter.
* **Escribirle por Telegram:** Abre tu bot en Telegram y envíale un mensaje. Responderá en el chat y mostrará su ejecución en vivo en la consola.

---

## 🕹️ Comandos Disponibles en Telegram

| Comando | Acción |
| :--- | :--- |
| `/start` | Mensaje de bienvenida, verificación de autorización y comandos disponibles |
| `/id` | Te devuelve tu ID numérico de Telegram para fácil copiado |
| `/status` | Estado actual del motor `agy`, ID de conversación activa y frecuencia del Heartbeat |
| `/reset` | Reinicia la memoria de la conversación en `agy` para empezar un contexto limpio |

---

## 🛡️ Personalización y Reglas (`AGENTS.md`)

El comportamiento, personalidad y restricciones del agente están definidos en [**`AGENTS.md`**](AGENTS.md). 

Puedes modificar este archivo en cualquier momento para:
* Cambiar el tono del asistente.
* Añadir reglas de seguridad (por ejemplo: *"Antes de tocar bases de datos o borrar ficheros, pide confirmación previa"*).
* Establecer preferencias de formato (respuestas resumidas, avisos con viñetas, etc.).

`agy` recarga y aplica estas directrices automáticamente en cada llamada.

---

## 🔄 Despliegue Permanente en Segundo Plano (`systemd`)

Si quieres que AgyAgent esté encendido las 24 horas del día como un verdadero demonio de sistema (incluso tras reiniciar el equipo o cerrar la sesión):

1. Crea el archivo de servicio del sistema:
   ```bash
   sudo nano /etc/systemd/system/agyagent.service
   ```

2. Pega la siguiente configuración:
   ```ini
   [Unit]
   Description=AgyAgent Autonomous AI Agent (Antigravity agy)
   After=network.target

   [Service]
   Type=simple
   User=tu_usuario
   WorkingDirectory=/ruta/a/tu/proyecto
   ExecStart=/ruta/a/tu/proyecto/.venv/bin/python bot.py
   Restart=always
   RestartSec=10
   Environment=PYTHONUNBUFFERED=1

   [Install]
   WantedBy=multi-user.target
   ```

3. Recarga y activa el servicio:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now agyagent
   ```

4. Para ver los logs en tiempo real del servicio:
   ```bash
   journalctl -u agyagent -f
   ```
