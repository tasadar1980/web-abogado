from flask import Flask, render_template, request, jsonify
from waitress import serve
import os
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
    print("[ENV] .env cargado explícitamente con python-dotenv y override=True.")
except ImportError:
    print("[ENV] python-dotenv no disponible. Se usarán variables del entorno del sistema.")

app = Flask(__name__)

CONTACT_LIMITS_FILE = Path(os.environ.get("CONTACT_LIMITS_FILE", "contact_limits.json"))
CONTACT_LIMIT_SECONDS = 30 * 24 * 60 * 60
CONTACT_LIMIT_MAX_MONTHLY = 2
RESEND_API_URL = "https://api.resend.com/emails"
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MIN_MENSAJE_UTIL = 30
MIN_NOMBRE_CARACTERES = 6
MIN_TELEFONO_DIGITOS = 8


def obtener_ip_cliente():
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "ip_desconocida"


def cargar_limites_contacto():
    if not CONTACT_LIMITS_FILE.exists():
        return {}

    try:
        with CONTACT_LIMITS_FILE.open("r", encoding="utf-8") as archivo:
            return json.load(archivo)
    except Exception as error:
        print(f"No se pudo leer contact_limits.json: {error}")
        return {}


def guardar_limites_contacto(limites):
    try:
        with CONTACT_LIMITS_FILE.open("w", encoding="utf-8") as archivo:
            json.dump(limites, archivo, ensure_ascii=False, indent=2)
    except Exception as error:
        print(f"No se pudo guardar contact_limits.json: {error}")


def validar_limite_mensual(ip_cliente):
    ahora = int(time.time())
    limites = cargar_limites_contacto()

    registro_actual = limites.get(ip_cliente)

    if isinstance(registro_actual, dict):
        primer_envio = int(registro_actual.get("primer_envio", 0) or 0)
        cantidad = int(registro_actual.get("cantidad", 0) or 0)
    else:
        primer_envio = int(registro_actual or 0)
        cantidad = 1 if primer_envio else 0

    if primer_envio and ahora - primer_envio >= CONTACT_LIMIT_SECONDS:
        primer_envio = 0
        cantidad = 0

    if cantidad >= CONTACT_LIMIT_MAX_MONTHLY:
        return False

    if not primer_envio:
        primer_envio = ahora

    limites[ip_cliente] = {
        "primer_envio": primer_envio,
        "cantidad": cantidad + 1
    }
    guardar_limites_contacto(limites)
    return True


def revertir_limite_mensual(ip_cliente):
    limites = cargar_limites_contacto()
    registro_actual = limites.get(ip_cliente)

    if not registro_actual:
        return

    if isinstance(registro_actual, dict):
        cantidad = int(registro_actual.get("cantidad", 0) or 0)
        if cantidad <= 1:
            limites.pop(ip_cliente, None)
        else:
            registro_actual["cantidad"] = cantidad - 1
            limites[ip_cliente] = registro_actual
    else:
        limites.pop(ip_cliente, None)

    guardar_limites_contacto(limites)


def validar_correo(correo):
    if not correo:
        return False
    patron = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    return re.match(patron, correo) is not None


def limpiar_espacios(texto):
    return re.sub(r"\s+", " ", (texto or "").strip())


def es_texto_repetitivo(texto):
    normalizado = re.sub(r"[^a-záéíóúñ0-9]", "", limpiar_espacios(texto).lower())
    if len(normalizado) < MIN_MENSAJE_UTIL:
        return False

    return len(set(normalizado)) <= 3


def mensaje_es_valido(mensaje):
    texto = limpiar_espacios(mensaje)
    return len(texto) >= MIN_MENSAJE_UTIL and not es_texto_repetitivo(texto)


def nombre_es_valido(nombre):
    return len(limpiar_espacios(nombre)) >= MIN_NOMBRE_CARACTERES


def telefono_es_valido(telefono):
    digitos = re.sub(r"\D", "", telefono or "")
    return len(digitos) >= MIN_TELEFONO_DIGITOS


def validar_datos_formulario(nombre, telefono, correo, mensaje, tipo="consulta"):
    if not mensaje_es_valido(mensaje):
        return f"Por favor describe tu {tipo} con al menos 30 caracteres útiles."

    if not nombre_es_valido(nombre):
        return "Por favor ingresa tu nombre completo o un nombre válido."

    if not telefono_es_valido(telefono):
        return "Por favor ingresa un teléfono o WhatsApp válido."

    if not validar_correo(correo):
        return "Por favor ingresa un correo válido."

    return None


def verificar_turnstile(token, ip_cliente):
    secret_key = os.environ.get("TURNSTILE_SECRET_KEY", "").strip()

    if not secret_key:
        print("TURNSTILE_SECRET_KEY no configurada. Verificación Turnstile omitida en este entorno.")
        return True

    if not token:
        print("[TURNSTILE] Validación detenida: token vacío o no recibido.")
        return False

    payload_turnstile = {
        "secret": secret_key,
        "response": token,
    }

    if ip_cliente not in ("127.0.0.1", "::1", "localhost"):
        payload_turnstile["remoteip"] = ip_cliente

    data = urllib.parse.urlencode(payload_turnstile).encode("utf-8")

    try:
        req = urllib.request.Request(TURNSTILE_VERIFY_URL, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:
            resultado = json.loads(response.read().decode("utf-8"))

        if not resultado.get("success"):
            print("Turnstile rechazado por Cloudflare.")
        else:
            print("Turnstile validado correctamente.")

        return bool(resultado.get("success"))
    except urllib.error.HTTPError as error:
        print(f"Error HTTP al verificar Turnstile: {error.code}")
        return False
    except Exception as error:
        print(f"Error al verificar Turnstile: {error}")
        return False


def enviar_email_resend(destinatario, asunto, texto, reply_to=None):
    resend_api_key = os.environ.get("RESEND_API_KEY", "").strip()
    remitente = os.environ.get("CONTACT_FROM_EMAIL", "no-responder@mail.bcta.cl").strip()

    if not resend_api_key:
        raise RuntimeError("RESEND_API_KEY no configurada.")

    payload = {
        "from": remitente,
        "to": [destinatario],
        "subject": asunto,
        "text": texto,
    }

    if reply_to:
        payload["reply_to"] = reply_to

    req = urllib.request.Request(
        RESEND_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {resend_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "BCTA-Web/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            respuesta = json.loads(response.read().decode("utf-8"))
            return respuesta
    except urllib.error.HTTPError as error:
        detalle = error.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Resend rechazó el envío: {error.code} - {detalle}") from error


@app.route("/")
def acceso():
    return render_template("index.html")


@app.route("/home")
def home():
    return render_template("index.html")


@app.route("/nosotros")
def nosotros():
    return render_template("nosotros.html")


@app.route("/servicios")
def servicios():
    return render_template("servicios.html")


@app.route("/contacto")
def contacto():
    return render_template(
        "contacto.html",
        turnstile_site_key=os.environ.get("TURNSTILE_SITE_KEY", "").strip()
    )


@app.route("/api/contacto", methods=["POST"])
def api_contacto():
    ip_cliente = obtener_ip_cliente()

    try:
        datos = request.get_json(silent=True) or {}
        area = datos.get("area", "No especificada").strip()
        mensaje = datos.get("mensaje", "").strip()
        nombre = datos.get("nombre", "").strip()
        telefono = datos.get("telefono", "").strip()
        correo = datos.get("correo", "").strip()
        honeypot = datos.get("website", "").strip()
        turnstile_token = datos.get("turnstileToken", "").strip()

        if honeypot:
            return jsonify({"status": "error", "message": "Solicitud no válida."}), 400

        error_validacion = validar_datos_formulario(nombre, telefono, correo, mensaje, "consulta")
        if error_validacion:
            return jsonify({"status": "error", "message": error_validacion}), 400

        if not verificar_turnstile(turnstile_token, ip_cliente):
            return jsonify({"status": "error", "message": "No se pudo validar la verificación de seguridad."}), 400

        if not validar_limite_mensual(ip_cliente):
            return jsonify({
                "status": "limit",
                "message": "Ya registramos el máximo de 2 consultas o solicitudes desde esta conexión durante este mes."
            }), 429

        destinatario = os.environ.get("CONTACT_TO_EMAIL", "contacto@bcta.cl").strip()
        reply_to_bcta = os.environ.get("CONTACT_REPLY_TO_EMAIL", "contacto@bcta.cl").strip()

        cuerpo_interno = f"""
NUEVA CONSULTA DESDE LA WEB BCTA
--------------------------------
Área de interés: {area}
Nombre: {nombre}
Teléfono: {telefono}
Correo: {correo}
IP registrada: {ip_cliente}

Mensaje:
{mensaje}
""".strip()

        cuerpo_cliente = f"""
Hola {nombre},

Hemos recibido tu consulta en BCTA Abogados.

Área seleccionada: {area}

Nuestro equipo revisará la información enviada y se pondrá en contacto contigo a la brevedad.

Este mensaje confirma únicamente la recepción de tu solicitud y no constituye asesoría legal ni aceptación de encargo profesional.

BCTA Abogados
contacto@bcta.cl
""".strip()

        enviar_email_resend(
            destinatario=destinatario,
            asunto=f"Nueva Consulta Web - {area} - {nombre}",
            texto=cuerpo_interno,
            reply_to=correo,
        )

        enviar_email_resend(
            destinatario=correo,
            asunto="Hemos recibido tu consulta | BCTA Abogados",
            texto=cuerpo_cliente,
            reply_to=reply_to_bcta,
        )

        return jsonify({"status": "success", "message": "Mensaje enviado correctamente."}), 200

    except Exception as error:
        revertir_limite_mensual(ip_cliente)
        print(f"Error real al procesar contacto: {error}")
        return jsonify({
            "status": "error",
            "message": "No pudimos enviar tu consulta en este momento. Por favor intenta nuevamente en unos minutos."
        }), 500


@app.route("/api/info", methods=["POST"])
def api_info():
    ip_cliente = obtener_ip_cliente()

    try:
        datos = request.get_json(silent=True) or {}
        area = datos.get("area", "No especificada").strip()
        mensaje = datos.get("mensaje", "").strip()
        nombre = datos.get("nombre", "").strip()
        telefono = datos.get("telefono", "").strip()
        correo = datos.get("correo", "").strip()
        honeypot = datos.get("website", "").strip()
        turnstile_token = datos.get("turnstileToken", "").strip()

        if honeypot:
            return jsonify({"status": "error", "message": "Solicitud no válida."}), 400

        error_validacion = validar_datos_formulario(nombre, telefono, correo, mensaje, "solicitud")
        if error_validacion:
            return jsonify({"status": "error", "message": error_validacion}), 400

        if not verificar_turnstile(turnstile_token, ip_cliente):
            return jsonify({"status": "error", "message": "No se pudo validar la verificación de seguridad."}), 400

        if not validar_limite_mensual(ip_cliente):
            return jsonify({
                "status": "limit",
                "message": "Ya registramos el máximo de 2 consultas o solicitudes desde esta conexión durante este mes."
            }), 429

        destinatario = os.environ.get(
            "INFO_TO_EMAIL",
            os.environ.get("CONTACT_TO_EMAIL", "contacto@bcta.cl")
        ).strip()
        reply_to_bcta = os.environ.get("CONTACT_REPLY_TO_EMAIL", "contacto@bcta.cl").strip()

        cuerpo_interno = f"""
NUEVA SOLICITUD DE INFORMACIÓN LEGAL DESDE LA WEB BCTA
------------------------------------------------------
Ítem seleccionado: {area}
Nombre: {nombre}
Teléfono: {telefono}
Correo: {correo}
IP registrada: {ip_cliente}

Mensaje:
{mensaje}
""".strip()

        cuerpo_cliente = f"""
Hola {nombre},

Hemos recibido tu solicitud de información en BCTA Abogados.

Ítem seleccionado: {area}

Nuestro equipo revisará la información enviada de acuerdo con su naturaleza y antecedentes disponibles.

Este mensaje confirma únicamente la recepción de tu solicitud y no constituye asesoría legal ni aceptación de encargo profesional.

BCTA Abogados
""".strip()

        enviar_email_resend(
            destinatario=destinatario,
            asunto=f"Nueva Solicitud de Información Legal - {area} - {nombre}",
            texto=cuerpo_interno,
            reply_to=correo,
        )

        enviar_email_resend(
            destinatario=correo,
            asunto="Hemos recibido tu solicitud de información | BCTA Abogados",
            texto=cuerpo_cliente,
            reply_to=reply_to_bcta,
        )

        return jsonify({"status": "success", "message": "Solicitud enviada correctamente."}), 200

    except Exception as error:
        revertir_limite_mensual(ip_cliente)
        print(f"Error real al procesar información legal: {error}")
        return jsonify({
            "status": "error",
            "message": "No pudimos enviar tu solicitud en este momento. Por favor intenta nuevamente en unos minutos."
        }), 500


@app.route("/usuarios")
def usuarios():
    return render_template("usuarios.html")


@app.route("/intranet")
def intranet():
    return render_template("intranet.html")


@app.route("/legal")
def legal():
    return render_template("legal.html")


@app.route("/info")
def info():
    return render_template(
        "info.html",
        turnstile_site_key=os.environ.get("TURNSTILE_SITE_KEY", "").strip()
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    serve(app, host="0.0.0.0", port=port)
