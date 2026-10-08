#!/usr/bin/env python3
"""
AgyAgent: Agente de IA Autónomo impulsado por Antigravity CLI (agy).
Usa tu cuenta activa de Google/Gemini sin costes de API Keys.
Soporta:
 - Mensajería remota por Telegram
 - Modo consola interactivo dual
 - Heartbeat periódico en segundo plano
 - Ejecución autónoma de herramientas de Linux (bash, web, ficheros)
"""

import os
import sys
import shutil
import asyncio
import logging
import json
import readline
from pathlib import Path
from dotenv import load_dotenv

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, TimedOut, TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "bot.log"
load_dotenv(BASE_DIR / ".env")

# Configuración de logs: guardar trazas en bot.log para no ensuciar la consola interactiva
logging.basicConfig(
    filename=LOG_FILE,
    filemode="a",
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger("agyagent")


def safe_console_print(text: str):
    """Imprime en la terminal sin cortar ni descolocar lo que el usuario esté escribiendo en el prompt."""
    try:
        # \r mueve el cursor al inicio y \033[K borra la línea actual
        sys.stdout.write("\r\033[K")
        sys.stdout.write(text + "\n")
        # Restaura el prompt con el texto que el usuario tenía escrito intacto
        readline.redisplay()
        sys.stdout.flush()
    except Exception:
        print(text, flush=True)


async def retry_telegram_call(coro_fn, *args, retries: int = 3, delay: float = 1.0, **kwargs):
    """Reintenta automáticamente llamadas asíncronas a Telegram ante microcortes de red transitorios."""
    for attempt in range(retries):
        try:
            return await coro_fn(*args, **kwargs)
        except (NetworkError, TimedOut, httpx.HTTPError) as err:
            if attempt < retries - 1:
                logger.warning(f"Reintentando llamada Telegram ({attempt + 1}/{retries}) tras corte de red: {err}")
                await asyncio.sleep(delay * (attempt + 1))
            else:
                logger.error(f"Fallo definitivo de red en Telegram tras {retries} intentos: {err}")
                raise


async def safe_reply(message, text: str, parse_mode: str = "Markdown"):
    """Envía una respuesta con reintentos y tolerancia a fallos de formato Markdown."""
    try:
        return await retry_telegram_call(message.reply_text, text, parse_mode=parse_mode)
    except BadRequest:
        return await retry_telegram_call(message.reply_text, text)

# Configuración
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0") or "0")
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "15") or "15")
ENABLE_HEARTBEAT = os.getenv("ENABLE_HEARTBEAT", "true").lower() in ("true", "1", "yes")

# Ruta del binario de agy
AGY_BIN = os.getenv("AGY_BIN", "").strip() or shutil.which("agy") or str(Path.home() / ".local/bin/agy")

# Lock global para evitar llamadas simultáneas a agy
AGY_LOCK = asyncio.Lock()

# Control de reinicio de conversación
CONVERSATION_ID: str | None = None


def check_auth(user_id: int) -> bool:
    """Verifica si el usuario está autorizado para operar el bot."""
    if ALLOWED_USER_ID == 0:
        return True
    return user_id == ALLOWED_USER_ID


def split_message(text: str, max_length: int = 4000) -> list[str]:
    """Divide mensajes largos para no exceder el límite de Telegram (4096 caracteres)."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = ""

    for line in lines:
        if len(current_chunk) + len(line) + 1 <= max_length:
            current_chunk += (line + "\n")
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            if len(line) > max_length:
                for i in range(0, len(line), max_length):
                    chunks.append(line[i : i + max_length])
                current_chunk = ""
            else:
                current_chunk = line + "\n"

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks or [text[:max_length]]


async def send_or_edit(bot, chat_id: int, text: str, edit_message_id: int | None = None):
    """Envía o edita texto en Telegram con fallback seguro y reintentos automáticos."""
    chunks = split_message(text)
    if not chunks:
        return

    first_chunk = chunks[0]

    if edit_message_id:
        try:
            try:
                await retry_telegram_call(
                    bot.edit_message_text,
                    chat_id=chat_id,
                    message_id=edit_message_id,
                    text=first_chunk,
                    parse_mode="Markdown",
                )
            except BadRequest:
                await retry_telegram_call(
                    bot.edit_message_text,
                    chat_id=chat_id,
                    message_id=edit_message_id,
                    text=first_chunk,
                )
            remaining_chunks = chunks[1:]
        except Exception as e:
            logger.warning(f"No se pudo editar mensaje {edit_message_id}: {e}. Enviando como nuevo mensaje.")
            remaining_chunks = chunks
    else:
        remaining_chunks = chunks

    for chunk in remaining_chunks:
        try:
            try:
                await retry_telegram_call(
                    bot.send_message,
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="Markdown",
                )
            except BadRequest:
                await retry_telegram_call(
                    bot.send_message,
                    chat_id=chat_id,
                    text=chunk,
                )
        except Exception as e:
            logger.error(f"Error definitivo enviando mensaje a Telegram chat {chat_id}: {e}")


# ====================================================================
# Motor agy (Ejecución mediante tu cuenta de Google en Antigravity)
# ====================================================================

async def execute_agy(prompt: str, is_heartbeat: bool = False, print_to_console: bool = True) -> str:
    """Ejecuta una petición usando el binario oficial de agy con streaming JSON."""
    global CONVERSATION_ID

    if not os.path.exists(AGY_BIN):
        return f"Error: No se encontró el binario de agy en {AGY_BIN}"

    async with AGY_LOCK:
        cmd = [
            AGY_BIN,
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
        ]

        # Continuar la conversación actual si ya existe
        if CONVERSATION_ID:
            cmd.extend(["--conversation", CONVERSATION_ID])
        else:
            cmd.append("-c")

        if print_to_console and not is_heartbeat:
            print("\n" + "─" * 65, flush=True)
            print(f"\033[1;36m🤖 AgyAgent [agy]\033[0m procesando: {prompt[:80]}...", flush=True)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        accumulated_text = ""
        full_response = ""

        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                    event = data.get("event")

                    if event == "init":
                        CONVERSATION_ID = data.get("conversation_id", CONVERSATION_ID)

                    elif event == "step_update":
                        su = data.get("step_update", {})
                        step_type = su.get("step_type", "")

                        # Notificar deltas de texto en terminal
                        if "text_delta" in su and print_to_console and not is_heartbeat:
                            delta = su["text_delta"]
                            accumulated_text += delta
                            sys.stdout.write(delta)
                            sys.stdout.flush()

                    elif event == "result":
                        res = data.get("result", {})
                        full_response = res.get("response", "")

                except json.JSONDecodeError:
                    pass

            await proc.wait()

            if print_to_console and not is_heartbeat:
                print("\n" + "─" * 65 + "\n", flush=True)

            return full_response or accumulated_text or "Tarea completada."

        except Exception as e:
            logger.exception("Error ejecutando agy:")
            return f"Error en ejecución de agy: {str(e)}"


# ====================================================================
# Heartbeat en segundo plano (Always-On)
# ====================================================================

async def heartbeat_loop(app: Application):
    """Latido periódico que revisa el sistema o tareas programadas en segundo plano."""
    if not ENABLE_HEARTBEAT or HEARTBEAT_MINUTES <= 0:
        logger.info("Heartbeat desactivado.")
        return

    logger.info(f"❤️ Heartbeat iniciado (intervalo: cada {HEARTBEAT_MINUTES} minutos).")
    await asyncio.sleep(15)  # Esperar 15 segundos al arranque inicial antes del primer pulso

    prompt_heartbeat = (
        "Instrucción de Heartbeat del sistema: Revisa de forma silenciosa el estado del sistema, "
        "alertas pendientes o recordatorios. "
        "Si todo está en orden y no hay nada importante que deba saber el usuario de inmediato, "
        "responde ÚNICAMENTE la palabra: SILENT_OK. "
        "Si hay alguna alerta o tarea relevante que deba conocer, redacta un aviso breve."
    )

    while True:
        try:
            await asyncio.sleep(HEARTBEAT_MINUTES * 60)
            logger.info("❤️ Pulso de Heartbeat ejecutándose...")
            result = await execute_agy(prompt_heartbeat, is_heartbeat=True, print_to_console=False)

            if "SILENT_OK" not in result and result.strip():
                logger.info("📢 Heartbeat detectó un aviso relevante. Notificando por Telegram...")
                safe_console_print(f"\033[1;33m📢 [AVISO DE HEARTBEAT]:\033[0m\n{result}")

                if ALLOWED_USER_ID != 0:
                    await app.bot.send_message(
                        chat_id=ALLOWED_USER_ID,
                        text=f"❤️ *Notificación de AgyAgent (Heartbeat):*\n\n{result}",
                        parse_mode="Markdown",
                    )
            else:
                logger.info("❤️ Heartbeat completado: Sistema en orden (SILENT_OK).")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Excepción en bucle de heartbeat: {e}")


# ====================================================================
# Consola Interactiva Dual (Escribir directamente en la terminal)
# ====================================================================

async def console_input_loop(app: Application):
    """Permite hablar con el agente escribiendo directamente en la terminal mientras el bot corre."""
    loop = asyncio.get_running_loop()
    print("\n\033[1;32m💬 Modo Consola Activo:\033[0m Puedes escribir aquí tus órdenes directamente en cualquier momento.")

    while True:
        try:
            user_input = await loop.run_in_executor(None, input, "\033[1;32m👤 Tú (Consola) > \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        prompt = user_input.strip()
        if not prompt:
            continue

        if prompt.lower() in ("exit", "quit", "salir"):
            print("Para detener el bot y el servidor por completo presiona Ctrl + C.")
            continue

        if prompt.lower() == "/reset":
            global CONVERSATION_ID
            CONVERSATION_ID = None
            print("\033[1;33m🧹 Memoria de sesión reiniciada en agy.\033[0m")
            continue

        # Procesar comando por agy
        await execute_agy(prompt, is_heartbeat=False, print_to_console=True)


# ====================================================================
# Handlers de Telegram
# ====================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja el comando /start en Telegram."""
    user = update.effective_user
    user_id = user.id if user else 0

    auth_status = "✅ *Autorizado*" if check_auth(user_id) else "⛔ *No Autorizado*"
    lock_note = ""
    if ALLOWED_USER_ID == 0:
        lock_note = (
            f"\n\n💡 *Aviso:* `TELEGRAM_ALLOWED_USER_ID` está en `0` (abierto).\n"
            f"Para asegurarlo pon en tu `.env`: `TELEGRAM_ALLOWED_USER_ID={user_id}`"
        )

    msg = (
        f"👋 ¡Hola, {user.first_name if user else 'usuario'}!\n\n"
        f"Soy **AgyAgent**, tu agente de IA autónomo impulsado por **Antigravity CLI (agy)**.\n\n"
        f"🆔 Tu User ID de Telegram: `{user_id}`\n"
        f"🔐 Estado: {auth_status}{lock_note}\n\n"
        f"🛠 *Comandos disponibles:*\n"
        f"• `/id` - Muestra tu ID de usuario de Telegram\n"
        f"• `/status` - Estado del motor agy y heartbeat\n"
        f"• `/reset` - Reinicia el hilo de la conversación\n\n"
        f"Escríbeme cualquier tarea directamente para que la ejecute en tu Linux."
    )
    await safe_reply(update.message, msg, parse_mode="Markdown")


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el ID de usuario de Telegram."""
    await safe_reply(
        update.message,
        f"Tu ID de usuario de Telegram es:\n`{update.effective_user.id}`",
        parse_mode="Markdown",
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el estado del agente y agy."""
    if not check_auth(update.effective_user.id):
        await safe_reply(update.message, "⛔ No autorizado.")
        return

    msg = (
        f"📊 *Estado de AgyAgent (Motor agy):*\n\n"
        f"• **Motor**: Antigravity CLI (`{AGY_BIN}`)\n"
        f"• **Autenticación**: Cuenta de Google de Antigravity (Sin costes de API Key)\n"
        f"• **Heartbeat**: {'🟢 Activo (cada ' + str(HEARTBEAT_MINUTES) + ' min)' if ENABLE_HEARTBEAT else '🔴 Desactivado'}\n"
        f"• **Conversación ID**: `{CONVERSATION_ID or 'Continuidad activa (-c)'}`\n"
        f"• **Directorio de trabajo**: `{BASE_DIR}`"
    )
    await safe_reply(update.message, msg, parse_mode="Markdown")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reinicia la conversación en agy."""
    if not check_auth(update.effective_user.id):
        await safe_reply(update.message, "⛔ No autorizado.")
        return

    global CONVERSATION_ID
    CONVERSATION_ID = None
    await safe_reply(update.message, "🧹 Memoria de conversación reiniciada en agy.")


async def keep_typing(bot, chat_id: int, stop_event: asyncio.Event):
    """Indicador periódico de escribiendo en Telegram."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja mensajes de texto entrantes de Telegram."""
    user = update.effective_user
    if not check_auth(user.id):
        await safe_reply(update.message, "⛔ No estás autorizado.")
        return

    user_text = update.message.text.strip()
    if not user_text:
        return

    status_msg = None
    try:
        status_msg = await safe_reply(update.message, "🧠 *Ejecutando en agy...*", parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"No se pudo enviar mensaje de espera temporal: {e}")

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(context.bot, update.effective_chat.id, stop_typing))

    # Notificar en consola sin romper el texto que el usuario esté tecleando
    safe_console_print(f"\033[1;36m💬 [TELEGRAM]\033[0m {user.first_name}: {user_text}")

    try:
        # print_to_console=False para no volcar tokens de Telegram en medio de lo que escribes en la terminal
        response_text = await execute_agy(user_text, is_heartbeat=False, print_to_console=False)

        stop_typing.set()
        await typing_task

        safe_console_print(f"\033[1;35m📤 [TELEGRAM RESPONDIDO]\033[0m a {user.first_name}")

        await send_or_edit(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            text=response_text,
            edit_message_id=status_msg.message_id if status_msg else None,
        )

    except Exception as e:
        stop_typing.set()
        await typing_task
        logger.exception("Error procesando mensaje:")
        err_msg = f"❌ *Error:*\n`{str(e)}`"
        try:
            if status_msg:
                await send_or_edit(
                    bot=context.bot,
                    chat_id=update.effective_chat.id,
                    text=err_msg,
                    edit_message_id=status_msg.message_id,
                )
            else:
                await safe_reply(update.message, err_msg)
        except Exception:
            pass


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Captura errores globales de la aplicación de Telegram evitando volcados en consola."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut, httpx.HTTPError)):
        logger.warning(f"Error de red temporal en Telegram ({type(err).__name__}): {err}")
    else:
        logger.error("Excepción no controlada en Telegram:", exc_info=err)


# ====================================================================
# Ciclo de vida y arranque
# ====================================================================

async def on_startup(app: Application):
    """Inicia las tareas en segundo plano (Heartbeat y Consola)."""
    # Lanzar Heartbeat
    app.bot_data["heartbeat_task"] = asyncio.create_task(heartbeat_loop(app))
    # Lanzar escucha de consola en segundo plano
    app.bot_data["console_task"] = asyncio.create_task(console_input_loop(app))


async def on_shutdown(app: Application):
    """Cancela tareas al apagar."""
    hb = app.bot_data.get("heartbeat_task")
    if hb:
        hb.cancel()
    ct = app.bot_data.get("console_task")
    if ct:
        ct.cancel()


def main():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "tu_token_aqui":
        print("\n❌ ERROR: Configura TELEGRAM_BOT_TOKEN en el archivo .env\n")
        sys.exit(1)

    print("\n" + "=" * 65)
    print("🤖 \033[1;32mAgyAgent - Motor agy (Antigravity CLI)\033[0m")
    print("• Autenticación: \033[1;34mSesión activa de Google / Antigravity (Cero API Key)\033[0m")
    print(f"• Binario agy: \033[1;34m{AGY_BIN}\033[0m")
    print(f"• Heartbeat: {'\033[1;32mActivo cada ' + str(HEARTBEAT_MINUTES) + ' min\033[0m' if ENABLE_HEARTBEAT else '\033[1;31mDesactivado\033[0m'}")
    print("• Escuchando en Telegram y en la Consola simultáneamente.")
    print("=" * 65)

    # Configurar cliente HTTP con timeouts generosos y reintentos automáticos
    req_config = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=15.0,
    )

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(req_config)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))

    app.run_polling(bootstrap_retries=10, timeout=30)


if __name__ == "__main__":
    main()
