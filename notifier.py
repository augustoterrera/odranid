"""
Notificador de errores por Telegram. Sin dependencias (stdlib), resiliente
(nunca lanza), no-op si no está configurado.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALERT_PROJECT
Prueba: python notifier.py "mensaje de prueba"
"""
import os
import json
import logging
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("notifier")
_API = "https://api.telegram.org/bot{token}/sendMessage"
_MAX = 4000


def _config():
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        os.environ.get("ALERT_PROJECT", "").strip() or "app",
    )


def enabled():
    token, chat, _ = _config()
    return bool(token and chat)


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(text, parse_mode="HTML"):
    token, chat, _ = _config()
    if not (token and chat):
        log.debug("telegram no configurado; alerta omitida")
        return False
    try:
        data = json.dumps({
            "chat_id": chat,
            "text": text[:_MAX],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            _API.format(token=token), data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log.warning("no pude enviar alerta a Telegram: %s", e)
        return False


def notify_error(titulo, detalle=None, contexto=None):
    _, _, proyecto = _config()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lineas = [f"🔴 <b>{_esc(proyecto)}</b> — {_esc(titulo)}", f"<i>{ts}</i>"]
    for k, v in (contexto or {}).items():
        lineas.append(f"• <b>{_esc(k)}:</b> {_esc(v)}")
    if detalle:
        lineas.append("")
        lineas.append(f"<pre>{_esc(str(detalle)[:1500])}</pre>")
    return send("\n".join(lineas))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level="INFO")
    msg = sys.argv[1] if len(sys.argv) > 1 else "prueba de alerta"
    if not enabled():
        print("Telegram NO configurado (faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        sys.exit(2)
    ok = notify_error("test de notificador", detalle=msg, contexto={"origen": "manual"})
    print("enviado OK" if ok else "fallo el envío (ver logs)")
    sys.exit(0 if ok else 1)
