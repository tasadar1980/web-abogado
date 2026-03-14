from flask import Flask, render_template, request, redirect, url_for, session
from waitress import serve
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)

SECRET_KEY = os.environ.get("SECRET_KEY")
CLAVE_ACCESO = os.environ.get("CLAVE_ACCESO")

if not SECRET_KEY:
    raise ValueError("Falta la variable de entorno SECRET_KEY")

if not CLAVE_ACCESO:
    raise ValueError("Falta la variable de entorno CLAVE_ACCESO")

app.secret_key = SECRET_KEY

MODO_CIERRE = True


@app.route("/", methods=["GET", "POST"])
def acceso():
    if session.get("autorizado"):
        return redirect(url_for("home"))

    error = None

    if request.method == "POST":
        password = request.form.get("password", "").strip()

        if password == CLAVE_ACCESO:
            session["autorizado"] = True
            return redirect(url_for("home"))
        else:
            error = "Contraseña incorrecta."

    return render_template(
        "index.html",
        mostrar_acceso=MODO_CIERRE,
        error=error
    )


@app.route("/home")
def home():
    if not session.get("autorizado"):
        return redirect(url_for("acceso"))

    return render_template(
        "index.html",
        mostrar_acceso=False,
        error=None
    )


@app.route("/usuarios")
def usuarios():
    if not session.get("autorizado"):
        return redirect(url_for("acceso"))
    return render_template("usuarios.html")


@app.route("/intranet")
def intranet():
    if not session.get("autorizado"):
        return redirect(url_for("acceso"))
    return render_template("intranet.html")


@app.route("/salir", methods=["POST"])
def salir():
    session.clear()
    return ("", 204)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    serve(app, host="0.0.0.0", port=port)
    