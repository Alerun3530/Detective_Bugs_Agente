"""
Servidor del agente — El Detective de Bugs
===========================================

Corre DENTRO de un contenedor en Railway (ver Dockerfile) — no en la PC
del estudiante. No hace falta instalar OpenCode ni nada localmente: se
instala en el build del contenedor.

Como no comparte disco con repo-victima (que corre en Render, en otro
lugar), el agente clona el repo de GitHub en cada corrida (o hace
git pull si ya lo tenía clonado), edita ese clone, y hace push. Ese
push dispara el auto-deploy de Render.

Uso local (para probar antes de deployar):
    python agent_server.py
"""

import json
import os
import subprocess
import uuid
import threading
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from openai import OpenAI
from supabase import create_client

load_dotenv()

app = Flask(__name__)


# ============================================================
# CONFIGURACIÓN
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Falta OPENAI_API_KEY en el .env / variables de Railway")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

MODELO_AGENTE = os.getenv("AGENTE_MODELO")
if not MODELO_AGENTE:
    raise RuntimeError("Falta AGENTE_MODELO en el .env / variables de Railway")

MODELO_SIMPLE = "gpt-4o-mini"
CANTIDAD_CHUNKS_RAG = 3
AGENT_TOKEN = os.getenv("AGENT_TOKEN")
TIMEOUT_AGENTE_SEG = 600
VENTANA_DEDUP_HORAS = int(os.getenv("VENTANA_DEDUP_HORAS", "2"))

# Carpeta DENTRO DEL CONTENEDOR donde se clona repo-victima. No es una
# carpeta de la PC de nadie — vive y muere con el contenedor.
REPO_PATH = os.getenv("REPO_PATH", "/app/repo-victima-clone")

# URL de git CON el token de GitHub embebido, para poder clonar y
# pushear sin que nadie tenga que loguearse a mano:
# https://<TOKEN>@github.com/tu-usuario/repo-victima.git
GITHUB_REPO_URL = os.getenv("GITHUB_REPO_URL")
if not GITHUB_REPO_URL:
    raise RuntimeError("Falta GITHUB_REPO_URL en el .env / variables de Railway")


# ============================================================
# GIT — clonar/actualizar y pushear
# ============================================================

def preparar_repo():
    if os.path.isdir(os.path.join(REPO_PATH, ".git")):
        subprocess.run(
            ["git", "-C", REPO_PATH, "pull"],
            check=True, capture_output=True, text=True,
        )
    else:
        subprocess.run(
            ["git", "clone", GITHUB_REPO_URL, REPO_PATH],
            check=True, capture_output=True, text=True,
        )


def git_commit_y_push(mensaje: str):
    subprocess.run(["git", "-C", REPO_PATH, "add", "-A"], check=True)
    commit = subprocess.run(
        ["git", "-C", REPO_PATH, "commit", "-m", mensaje],
        capture_output=True, text=True,
    )
    # Si el agente no tocó ningún archivo (solo_diagnostico), git commit
    # falla con "nothing to commit" — no es un error real.
    if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
        raise RuntimeError(f"git commit falló: {commit.stdout}\n{commit.stderr}")
    if commit.returncode == 0:
        subprocess.run(
            ["git", "-C", REPO_PATH, "push"],
            check=True, capture_output=True, text=True,
        )


# ============================================================
# OPENAI — EMBEDDINGS
# ============================================================

def get_embedding(texto: str):
    r = openai_client.embeddings.create(model="text-embedding-3-small", input=texto)
    return r.data[0].embedding


# ============================================================
# RAG
# ============================================================

def consultar_rag(descripcion_error: str):
    embedding = get_embedding(descripcion_error)
    contexto = supabase.rpc(
        "match_documents_bugs",
        {"query_embedding": embedding, "match_count": CANTIDAD_CHUNKS_RAG, "filter": {}},
    ).execute()
    incidentes = supabase.rpc(
        "match_incidentes",
        {"query_embedding": embedding, "match_count": CANTIDAD_CHUNKS_RAG, "filter": {}},
    ).execute()
    return contexto.data or [], incidentes.data or []


# ============================================================
# CREAR PROMPT
# ============================================================

def armar_prompt_final(payload, contexto_proyecto, incidentes_similares):
    descripcion = payload.get("descripcion") or payload.get("stack_trace") or payload.get("error") or ""
    endpoint = payload.get("endpoint", "")
    severity = payload.get("severity", "")

    contexto_texto = "\n".join(f"- {c['content']}" for c in contexto_proyecto[:3]) or "Sin contexto adicional."
    incidentes_texto = "\n".join(f"- {i['content']}" for i in incidentes_similares[:2]) or "Sin incidentes similares."

    prompt_final = f"""
Actuá como agente autónomo de código para "El Detective de Bugs".

REPOSITORIO:
{REPO_PATH}

BUG A SOLUCIONAR:
Endpoint: {endpoint}
Severidad: {severity}

Error:
{descripcion}

CONTEXTO:
{contexto_texto}

INCIDENTES SIMILARES:
{incidentes_texto}

TAREA:

Solucioná ÚNICAMENTE el bug indicado en el error recibido.

1. Trabajá únicamente sobre el bug reportado.
2. No busques ni arregles otros bugs.
3. No revises README_BUGS.md para buscar tareas adicionales.
4. Podés leer los archivos de código necesarios para encontrar la causa raíz.
5. Aplicá directamente el cambio en el repositorio.
6. No hagas refactorizaciones ni mejoras que no sean necesarias para solucionar este bug.
7. Si el monitor reporta una anomalía, investigá su causa en el código y solucionála.
8. No delegues el problema simplemente porque parezca una regla de negocio.
9. Verificá que el cambio realizado solucione el bug recibido.
10. Si encontrás otros errores durante la investigación, ignorálos.
11. No modifiques dependencias ni archivos fuera del repositorio.
12. El bug recibido es la única tarea.
13. "accion_tomada" NUNCA puede ser "escalado" — esa opción no existe en
    este proyecto. Si el bug involucra una regla de negocio ambigua,
    igual aplicá el fix, y marcá "confianza": "baja" explicando el
    riesgo en "resumen_para_humano". Nunca dejás un bug sin tocar el código.
14. Si el bug recibido es Bug 3, debés investigarlo y aplicar el
    arreglo correspondiente en el código. No lo escales simplemente
    porque involucre una regla de negocio — usá confianza baja.

Al terminar, respondé ÚNICAMENTE con JSON válido:

{{
  "diagnostico": "causa raíz del error",
  "accion_tomada": "fix_aplicado o solo_diagnostico",
  "confianza": "alta, media o baja",
  "archivo_modificado": "ruta del archivo",
  "resumen_para_humano": "qué cambiaste y por qué"
}}
""".strip()

    return prompt_final


# ============================================================
# EJECUTAR OPENCODE
# ============================================================

def llamar_agente(prompt_final: str, job_id: str) -> dict:
    entorno = os.environ.copy()
    home_aislado = f"/tmp/opencode-home-{job_id}"
    os.makedirs(home_aislado, exist_ok=True)
    entorno["HOME"] = home_aislado

    try:
        resultado = subprocess.run(
            ["opencode", "run", "--agent", "detective-de-bugs", "--model", MODELO_AGENTE, "--format", "json"],
            input=prompt_final,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_AGENTE_SEG,
            cwd=REPO_PATH,
            env=entorno,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"El agente no terminó en {TIMEOUT_AGENTE_SEG} segundos.")
    except FileNotFoundError as e:
        raise RuntimeError(f"No se encontró 'opencode' en el PATH del contenedor. Detalle: {e}")

    print("RETURN CODE:", resultado.returncode)
    print("STDOUT:", resultado.stdout)
    print("STDERR:", resultado.stderr)

    if resultado.returncode != 0:
        raise RuntimeError(f"OpenCode terminó con error.\nSTDOUT:\n{resultado.stdout}\nSTDERR:\n{resultado.stderr}")

    # Intentamos extraer el JSON de la respuesta, pero un fallo acá
    # NUNCA debe impedir que se pusheen los cambios ya aplicados.
    try:
        return _parsear_respuesta_agente(resultado.stdout)
    except RuntimeError as e:
        print(f"[{job_id}] No se pudo parsear el JSON del agente: {e}", flush=True)
        # Devolvemos un resultado "genérico" en vez de explotar,
        # para que el caller decida el push según el estado real del repo.
        return {
            "diagnostico": "No se pudo extraer diagnóstico (el agente no devolvió JSON válido).",
            "accion_tomada": "desconocido",
            "confianza": "baja",
            "archivo_modificado": None,
            "resumen_para_humano": "El agente aplicó cambios pero no devolvió un JSON parseable. Ver logs.",
        }


def _parsear_respuesta_agente(stdout: str) -> dict:
    eventos = []
    for linea in stdout.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        try:
            eventos.append(json.loads(linea))
        except json.JSONDecodeError:
            continue

    textos = []
    for evento in eventos:
        if evento.get("type") != "text":
            continue
        texto = evento.get("part", {}).get("text")
        if texto:
            textos.append(texto.strip())

    if not textos:
        raise RuntimeError(f"OpenCode no devolvió texto.\nSalida completa:\n{stdout}")

    respuesta_texto = textos[-1]

    try:
        return json.loads(respuesta_texto)
    except json.JSONDecodeError:
        texto_limpio = respuesta_texto.strip()
        if texto_limpio.startswith("```json"):
            texto_limpio = texto_limpio[7:]
        elif texto_limpio.startswith("```"):
            texto_limpio = texto_limpio[3:]
        if texto_limpio.endswith("```"):
            texto_limpio = texto_limpio[:-3]
        try:
            return json.loads(texto_limpio.strip())
        except json.JSONDecodeError as e:
            raise RuntimeError(f"El agente no devolvió JSON válido: {e}\nRespuesta:\n{respuesta_texto}")


# ============================================================
# DEDUP + PERSISTENCIA
# ============================================================

def incidente_reciente(endpoint: str):
    if not endpoint:
        return None
    desde = (datetime.now(timezone.utc) - timedelta(hours=VENTANA_DEDUP_HORAS)).isoformat()
    r = (
        supabase.table("incidentes")
        .select("metadata")
        .eq("metadata->>endpoint", endpoint)
        .gte("metadata->>timestamp", desde)
        .order("metadata->>timestamp", desc=True)
        .limit(1)
        .execute()
    )
    return r.data[0]["metadata"] if r.data else None


def guardar_solucion(payload: dict, resultado_agente: dict):
    texto = (
        f"Error: {payload.get('descripcion')}\n"
        f"Endpoint: {payload.get('endpoint')}\n"
        f"Diagnóstico: {resultado_agente.get('diagnostico')}\n"
        f"Acción tomada: {resultado_agente.get('accion_tomada')}\n"
        f"Resumen: {resultado_agente.get('resumen_para_humano')}"
    )
    embedding = get_embedding(texto)
    supabase.table("incidentes").insert({
        "content": texto,
        "embedding": embedding,
        "metadata": {
            "error_id": payload.get("error_id"),
            "timestamp": payload.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            "endpoint": payload.get("endpoint"),
            "severity": payload.get("severity"),
            "origen": payload.get("origen"),
            "diagnostico": resultado_agente.get("diagnostico"),
            "accion_tomada": resultado_agente.get("accion_tomada"),
            "confianza": resultado_agente.get("confianza"),
            "archivo_modificado": resultado_agente.get("archivo_modificado"),
            "resumen_para_humano": resultado_agente.get("resumen_para_humano"),
            "estado": "resuelto" if resultado_agente.get("accion_tomada") == "fix_aplicado" else "pendiente",
        },
    }).execute()


trabajos = {}


def hay_cambios_sin_commitear() -> bool:
    resultado = subprocess.run(
        ["git", "-C", REPO_PATH, "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return bool(resultado.stdout.strip())


def ejecutar_agente_en_segundo_plano(payload, contexto_proyecto, incidentes_similares, job_id):
    try:
        print(f"[{job_id}] git clone/pull...", flush=True)
        preparar_repo()

        print(f"[{job_id}] Armando prompt...", flush=True)
        prompt_final = armar_prompt_final(payload, contexto_proyecto, incidentes_similares)
        print(f"[{job_id}] PROMPT COMPLETO:\n{prompt_final}", flush=True)
        print(f"[{job_id}] Ejecutando OpenCode...", flush=True)

        resultado_agente = llamar_agente(prompt_final, job_id)

        # Pusheamos según el ESTADO REAL DEL REPO, no según lo que diga
        # el JSON (que puede venir mal parseado igual habiendo cambios).
        if hay_cambios_sin_commitear():
            print(f"[{job_id}] Hay cambios en el repo → git commit + push...", flush=True)
            mensaje = f"fix: {resultado_agente.get('diagnostico', 'fix automático del agente')[:72]}"
            git_commit_y_push(mensaje)
            # Si el JSON no traía accion_tomada clara pero SÍ hubo cambios reales,
            # lo marcamos como fix_aplicado para que quede bien registrado.
            if resultado_agente.get("accion_tomada") not in ("fix_aplicado", "solo_diagnostico"):
                resultado_agente["accion_tomada"] = "fix_aplicado"
        else:
            print(f"[{job_id}] Sin cambios en el repo, no se pushea.", flush=True)

        guardar_solucion(payload, resultado_agente)
        print(f"[{job_id}] Listo.", flush=True)

        trabajos[job_id] = {"estado": "completado", "resultado": resultado_agente}
    except Exception as e:
        print(f"[{job_id}] ERROR: {e}", flush=True)
        trabajos[job_id] = {"estado": "error", "error": str(e)}

# ============================================================
# ENDPOINTS
# ============================================================

@app.route("/procesar", methods=["POST"])
def procesar():
    if not AGENT_TOKEN:
        return jsonify({"error": "AGENT_TOKEN no está definido"}), 500
    if request.headers.get("X-Agent-Token") != AGENT_TOKEN:
        return jsonify({"error": "token inválido"}), 401

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "body inválido"}), 400

    descripcion = payload.get("descripcion") or payload.get("stack_trace") or payload.get("error")
    if not descripcion:
        return jsonify({"error": "falta 'descripcion' o 'stack_trace' en el body"}), 400
    payload["descripcion"] = descripcion

    try:
        previo = incidente_reciente(payload.get("endpoint"))
        if previo:
            return jsonify({
                "skip": f"ya hay un incidente para {payload.get('endpoint')} en las últimas {VENTANA_DEDUP_HORAS} h",
                "incidente_previo": previo,
            }), 200

        contexto_proyecto, incidentes_similares = consultar_rag(payload["descripcion"])

        job_id = str(uuid.uuid4())
        trabajos[job_id] = {"estado": "procesando"}

        hilo = threading.Thread(
            target=ejecutar_agente_en_segundo_plano,
            args=(payload, contexto_proyecto, incidentes_similares, job_id),
            daemon=True,
        )
        hilo.start()

        return jsonify({
            "estado": "procesando",
            "job_id": job_id,
            "mensaje": "El error fue recibido. El agente está investigando el repositorio.",
        }), 202
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/trabajos/<job_id>", methods=["GET"])
def ver_trabajo(job_id):
    trabajo = trabajos.get(job_id)
    if not trabajo:
        return jsonify({"error": "job_id no encontrado"}), 404
    return jsonify(trabajo)


@app.route("/salud", methods=["GET"])
def salud():
    return jsonify({"ok": True, "servicio": "agente-detective", "modelo": MODELO_AGENTE})


if __name__ == "__main__":
    print(f"Servidor del agente corriendo en el puerto {os.getenv('PORT', 5000)}")
    print(f"Modelo del agente: {MODELO_AGENTE}")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
