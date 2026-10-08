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

async def execute_agy(
    prompt: str,
    source: str = "CONSOLA",
    sender: str = "Tú",
    is_heartbeat: bool = False,
) -> str:
    """Ejecuta una petición usando el binario oficial de agy mostrando toda la actividad (pensamiento, herramientas, streaming)."""
    global CONVERSATION_ID

    if not os.path.exists(AGY_BIN):
        err_msg = f"Error: No se encontró el binario de agy en {AGY_BIN}"
        if not is_heartbeat:
            safe_console_print(f"\033[1;31m❌ {err_msg}\033[0m")
        return err_msg

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

        # Preparar la consola (limpiar la línea de prompt para mostrar la actividad limpia)
        buf = ""
        if not is_heartbeat:
            try:
                buf = readline.get_line_buffer()
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
            except Exception:
                pass

            print("\n" + "═" * 65, flush=True)
            if source == "TELEGRAM":
                print(f"💬 \033[1;36m[MENSAJE TELEGRAM]\033[0m \033[1m{sender}:\033[0m {prompt}", flush=True)
            else:
                print(f"👤 \033[1;32m[ORDEN CONSOLA]:\033[0m {prompt}", flush=True)
            print(f"🤖 \033[1;35mAgyAgent [agy]\033[0m ejecutando...", flush=True)
            print("─" * 65, flush=True)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        accumulated_text = ""
        full_response = ""
        thought_announced = False
        reply_announced = False

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
                        state = su.get("state", "")

                        if not is_heartbeat:
                            # 1. Herramientas del sistema (bash, ficheros, web, etc.)
                            if step_type == "tool":
                                tool_name = su.get("tool_name") or su.get("tool_info", {}).get("name", "herramienta")
                                tool_info = su.get("tool_info", {})

                                if state == "ACTIVE":
                                    params = tool_info.get("parameters", {})
                                    param_strs = []
                                    for k, v in params.items():
                                        v_str = str(v).replace("\n", " ")
                                        if len(v_str) > 80:
                                            v_str = v_str[:77] + "..."
                                        param_strs.append(f"{k}={v_str}")
                                    p_display = ", ".join(param_strs) if param_strs else ""
                                    print(f"\n\033[1;33m🛠️  [HERRAMIENTA: {tool_name}]\033[0m {p_display}", flush=True)

                                elif state == "DONE":
                                    dur = su.get("duration_seconds", 0)
                                    out = str(tool_info.get("output", "")).strip()
                                    if out:
                                        out_lines = out.split("\n")
                                        preview = out_lines[0] if len(out_lines) == 1 else f"{out_lines[0]} ... ({len(out_lines)} líneas)"
                                        if len(preview) > 100:
                                            preview = preview[:97] + "..."
                                        print(f"\033[0;33m   └─ Salida ({dur:.2f}s):\033[0m {preview}", flush=True)
                                    else:
                                        print(f"\033[0;33m   └─ Completado en {dur:.2f}s\033[0m", flush=True)

                            # 2. Pensamiento / Razonamiento
                            elif step_type == "agent_response":
                                if "thought_delta" in su:
                                    if not thought_announced:
                                        print(f"\n\033[0;35m🧠 [PENSAMIENTO / RAZONAMIENTO]:\033[0m", flush=True)
                                        thought_announced = True
                                    sys.stdout.write(f"\033[0;90m{su['thought_delta']}\033[0m")
                                    sys.stdout.flush()

                                elif "thought" in su and su["thought"]:
                                    print(f"\n\033[0;90m🧠 [Pensamiento]: {su['thought']}\033[0m", flush=True)

                                usage = su.get("usage", {})
                                if usage.get("thinking_tokens", 0) > 0 and not thought_announced:
                                    print(f"\n\033[0;35m🧠 [RAZONAMIENTO GEMINI]\033[0m ({usage['thinking_tokens']} tokens de pensamiento)", flush=True)
                                    thought_announced = True

                                # 3. Streaming de respuesta en tiempo real
                                if "text_delta" in su:
                                    delta = su["text_delta"]
                                    accumulated_text += delta
                                    if not reply_announced:
                                        print(f"\n\033[1;32m💬 [RESPUESTA DE AGYAGENT]:\033[0m", flush=True)
                                        reply_announced = True
                                    sys.stdout.write(delta)
                                    sys.stdout.flush()

                        else:
                            # Acumular texto para Heartbeat silencioso
                            if "text_delta" in su:
                                accumulated_text += su["text_delta"]

                    elif event == "result":
                        res = data.get("result", {})
                        full_response = res.get("response", "")
                        usage = res.get("usage", {})
                        if not is_heartbeat and usage:
                            in_tok = usage.get("input_tokens", 0)
                            out_tok = usage.get("output_tokens", 0)
                            th_tok = usage.get("thinking_tokens", 0)
                            print(f"\n\033[0;90m📊 Tokens: Entrada: {in_tok} | Salida: {out_tok} | Razonamiento: {th_tok}\033[0m", flush=True)

                except json.JSONDecodeError:
                    pass

            await proc.wait()

            final_text = full_response or accumulated_text or "Tarea completada."

            if not is_heartbeat:
                print("\n" + "═" * 65, flush=True)
                if source == "TELEGRAM":
                    print(f"\033[1;35m📤 [RESPUESTA ENVIADA A TELEGRAM]\033[0m a {sender}\n", flush=True)
                    try:
                        readline.redisplay()
                    except Exception:
                        pass
                elif source == "CONSOLA":
                    print(f"\033[1;32m✅ [COMPLETADO EN CONSOLA]\033[0m\n", flush=True)

            return final_text

        except Exception as e:
            logger.exception("Error ejecutando agy:")
            err_str = f"Error en ejecución de agy: {str(e)}"
            if not is_heartbeat:
                print(f"\n\033[1;31m❌ {err_str}\033[0m\n", flush=True)
            return err_str


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
            safe_console_print("\033[1;33m❤️ [HEARTBEAT]\033[0m Comprobando estado del sistema en segundo plano...")
            result = await execute_agy(prompt_heartbeat, source="HEARTBEAT", sender="Heartbeat", is_heartbeat=True)

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
                safe_console_print("\033[0;90m❤️ [HEARTBEAT] Sistema en orden (SILENT_OK).\033[0m")

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
        await execute_agy(prompt, source="CONSOLA", sender="Tú", is_heartbeat=False)


# ====================================================================
# Handlers de Telegram
# ====================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja el comando /start en Telegram."""
    user = update.effective_user
    user_id = user.id if user else 0
    safe_console_print(f"\033[1;36m📌 [TELEGRAM COMANDO]\033[0m {user.first_name if user else 'desconocido'}: /start")

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
    """Maneja el comando /id en Telegram."""
    user = update.effective_user
    safe_console_print(f"\033[1;36m📌 [TELEGRAM COMANDO]\033[0m {user.first_name if user else 'desconocido'}: /id")
    await safe_reply(
        update.message,
        f"Tu ID de usuario de Telegram es:\n`{update.effective_user.id}`",
        parse_mode="Markdown",
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el estado del agente y agy."""
    user = update.effective_user
    safe_console_print(f"\033[1;36m📌 [TELEGRAM COMANDO]\033[0m {user.first_name if user else 'desconocido'}: /status")
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
    user = update.effective_user
    safe_console_print(f"\033[1;33m📌 [TELEGRAM COMANDO]\033[0m {user.first_name if user else 'desconocido'}: /reset")
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
        safe_console_print(f"\033[1;31m⛔ [TELEGRAM NO AUTORIZADO]\033[0m Usuario {user.id} ({user.first_name}) intentó interactuar.")
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

    try:
        # Ejecutar en agy con salida detallada completa por consola (pensamiento, herramientas, streaming)
        response_text = await execute_agy(user_text, source="TELEGRAM", sender=user.first_name, is_heartbeat=False)

        stop_typing.set()
        await typing_task

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
