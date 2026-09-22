"""
Servidor local del agente — El Detective de Bugs

Corre en tu máquina, en la MISMA carpeta que sirve repo-victima.

Uso:
    python agent_server.py

En otra terminal:
    cloudflared tunnel --url http://localhost:5000
"""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
import threading
import uuid
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
    raise RuntimeError(
        "Falta OPENAI_API_KEY en el archivo .env"
    )

openai_client = OpenAI(
    api_key=OPENAI_API_KEY
)

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)


# Modelo que OpenCode utilizará.
# Se toma directamente del .env.
MODELO_AGENTE = os.getenv("AGENTE_MODELO")

if not MODELO_AGENTE:
    raise RuntimeError(
        "Falta AGENTE_MODELO en el archivo .env"
    )


# Modelo OpenAI usado para generar el briefing.
MODELO_SIMPLE = "gpt-4o-mini"

CANTIDAD_CHUNKS_RAG = 3

AGENT_TOKEN = os.getenv("AGENT_TOKEN")

TIMEOUT_AGENTE_SEG = 600

VENTANA_DEDUP_HORAS = int(
    os.getenv("VENTANA_DEDUP_HORAS", "2")
)


# OpenCode instalado en Windows
OPENCODE_CMD = r"C:\nvm4w\nodejs\opencode.CMD"


# ============================================================
# OPENAI — EMBEDDINGS
# ============================================================

def get_embedding(texto: str):

    r = openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=texto
    )

    return r.data[0].embedding


# ============================================================
# RAG
# ============================================================

def consultar_rag(descripcion_error: str):

    embedding = get_embedding(descripcion_error)

    contexto = supabase.rpc(
        "match_documents_bugs",
        {
            "query_embedding": embedding,
            "match_count": CANTIDAD_CHUNKS_RAG,
            "filter": {}
        }
    ).execute()

    incidentes = supabase.rpc(
        "match_incidentes",
        {
            "query_embedding": embedding,
            "match_count": CANTIDAD_CHUNKS_RAG,
            "filter": {}
        }
    ).execute()

    return (
        contexto.data or [],
        incidentes.data or []
    )


# ============================================================
# CREAR PROMPT
# ============================================================

def armar_prompt_final(
    payload,
    contexto_proyecto,
    incidentes_similares,
    repo_path
):
    descripcion = (
        payload.get("descripcion")
        or payload.get("stack_trace")
        or payload.get("error")
        or ""
    )

    endpoint = payload.get("endpoint", "")
    severity = payload.get("severity", "")

    contexto_texto = "\n".join(
        f"- {c['content']}"
        for c in contexto_proyecto[:3]
    ) or "Sin contexto adicional."

    incidentes_texto = "\n".join(
        f"- {i['content']}"
        for i in incidentes_similares[:2]
    ) or "Sin incidentes similares."

    print("\n========== DATOS DEL ERROR ==========")
    print(f"Endpoint: {endpoint}")
    print(f"Severidad: {severity}")
    print(f"Descripción:\n{descripcion}")
    print("=====================================\n")

    prompt_final = f"""
Actuá como agente autónomo de código para "El Detective de Bugs".

REPOSITORIO:
{repo_path}

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
    este proyecto. Si el bug involucra una regla de negocio ambigua
    (como el Bug 3), igual aplicá el fix, y marcá "confianza": "baja"
    explicando el riesgo en "resumen_para_humano". Nunca dejás un bug
    sin tocar el código.
14. Si el bug recibido es Bug 3, debés investigarlo y aplicar el
    arreglo correspondiente en el código. No lo escales simplemente
    porque involucre una regla de negocio — usá confianza baja en su
    lugar.

15. Si te llega un bug de negocio que esos bugs los maneja el endpoind de anomalias deja el endpoint de anomalias SIN NINGUNA DETECCION, LIMPIALO.
Usá comandos compatibles con Windows PowerShell.

Al terminar, respondé ÚNICAMENTE con JSON válido:

{{
  "diagnostico": "causa raíz del error",
  "accion_tomada": "fix_aplicado o solo_diagnostico",
  "confianza": "alta, media o baja",
  "archivo_modificado": "ruta del archivo",
  "resumen_para_humano": "qué cambiaste y por qué"
}}
""".strip()

    print("\n========== PROMPT FINAL ==========")
    print(prompt_final)
    print("==================================\n")

    return prompt_final
# ============================================================
# EJECUTAR OPENCODE
# ============================================================

def llamar_agente(prompt_final: str, repo_path: str) -> dict:

    try:
        entorno = os.environ.copy()
        entorno["OPENAI_API_KEY"] = OPENAI_API_KEY

        resultado = subprocess.run(
    [
        OPENCODE_CMD,
        "run",
        "--agent",
        "detective-de-bugs",
        "--model",
        MODELO_AGENTE,
        "--format",
        "json"
        # OJO: el prompt YA NO va acá como argumento. Con shell=True en
        # Windows, un texto largo y multilínea con comillas adentro (como
        # el ejemplo de JSON que le pedimos al final del prompt) se
        # rompe al pasar por DOS parseos de shell (cmd.exe + el .CMD de
        # OpenCode), y el agente terminaba recibiendo solo un fragmento
        # vacío — por eso respondía "no hay ningún error reportado".
        # En vez de eso, se lo mandamos por stdin con "input=" de
        # subprocess.run: OpenCode lee stdin hasta EOF antes de arrancar
        # (está documentado así), y como no pasa por ningún parser de
        # shell, no hay comillas que rompan nada.
    ],
    input=prompt_final,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=TIMEOUT_AGENTE_SEG,
    cwd=repo_path,
    shell=True,
    env=entorno
)

    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"El agente no terminó en "
            f"{TIMEOUT_AGENTE_SEG} segundos."
        )

    except FileNotFoundError as e:
        raise RuntimeError(
            f"No se encontró OpenCode en:\n"
            f"{OPENCODE_CMD}\n\n"
            f"Detalle: {e}"
        )

    print("\n========== OPENCODE ==========")
    print("MODELO:", MODELO_AGENTE)
    print("RETURN CODE:", resultado.returncode)

    print("\n--- STDOUT ---")
    print(resultado.stdout)

    print("\n--- STDERR ---")
    print(resultado.stderr)

    print("========== FIN OPENCODE ==========\n")

    if resultado.returncode != 0:
        raise RuntimeError(
            "OpenCode terminó con error.\n\n"
            f"RETURN CODE: {resultado.returncode}\n\n"
            f"STDOUT:\n{resultado.stdout}\n\n"
            f"STDERR:\n{resultado.stderr}"
        )

    eventos = []

    for linea in resultado.stdout.splitlines():
        linea = linea.strip()

        if not linea:
            continue

        try:
            evento = json.loads(linea)
            eventos.append(evento)

        except json.JSONDecodeError:
            continue

    # ---------------------------------------------------------
    # BUSCAR TEXTOS DEL AGENTE
    # ---------------------------------------------------------

    textos = []

    for evento in eventos:

        if evento.get("type") != "text":
            continue

        part = evento.get("part", {})
        texto = part.get("text")

        if texto:
            textos.append(texto.strip())

    # ---------------------------------------------------------
    # SI HAY TEXTO, INTENTAMOS LEER EL JSON DEL AGENTE
    # ---------------------------------------------------------

    if textos:

        respuesta_texto = textos[-1]

        print("\n========== RESPUESTA DEL AGENTE ==========")
        print(respuesta_texto)
        print("==========================================\n")

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

            texto_limpio = texto_limpio.strip()

            try:
                return json.loads(texto_limpio)

            except json.JSONDecodeError as e:

                raise RuntimeError(
                    "OpenCode terminó correctamente, "
                    "pero el agente no devolvió el JSON esperado.\n\n"
                    f"Error JSON: {e}\n\n"
                    f"Respuesta del agente:\n{respuesta_texto}"
                )

    # ---------------------------------------------------------
    # SI NO HAY TEXTO, ANALIZAMOS LOS EVENTOS
    # ---------------------------------------------------------

    herramientas = []

    for evento in eventos:

        if evento.get("type") != "tool_use":
            continue

        part = evento.get("part", {})

        herramienta = part.get("tool")
        call_id = part.get("callID")

        state = part.get("state", {})

        herramientas.append({
            "tool": herramienta,
            "call_id": call_id,
            "status": state.get("status"),
            "title": state.get("title"),
            "error": state.get("error")
        })

    print("\n========== RESUMEN DE HERRAMIENTAS ==========")

    for herramienta in herramientas:
        print(herramienta)

    print("==============================================\n")

    if herramientas:

        ultima = herramientas[-1]

        raise RuntimeError(
            "OpenCode terminó sin devolver una respuesta de texto.\n\n"
            f"Última herramienta utilizada: {ultima.get('tool')}\n"
            f"Estado: {ultima.get('status')}\n"
            f"Título: {ultima.get('title')}\n"
            f"Error: {ultima.get('error')}\n\n"
            "Revisa la salida completa de OpenCode para determinar "
            "por qué el agente no terminó su respuesta."
        )

    raise RuntimeError(
        "OpenCode terminó correctamente, "
        "pero no devolvió texto ni eventos de herramientas."
    )
# ============================================================
# EVITAR INCIDENTES REPETIDOS
# ============================================================

def incidente_reciente(endpoint: str):

    if not endpoint:
        return None

    desde = (
        datetime.now(timezone.utc)
        - timedelta(hours=VENTANA_DEDUP_HORAS)
    ).isoformat()

    r = (
        supabase.table("incidentes")
        .select("metadata")
        .eq(
            "metadata->>endpoint",
            endpoint
        )
        .gte(
            "metadata->>timestamp",
            desde
        )
        .order(
            "metadata->>timestamp",
            desc=True
        )
        .limit(1)
        .execute()
    )

    return (
        r.data[0]["metadata"]
        if r.data
        else None
    )


# ============================================================
# GUARDAR SOLUCIÓN
# ============================================================

def guardar_solucion(
    payload: dict,
    resultado_agente: dict
):

    texto = (
        f"Error: "
        f"{payload.get('descripcion')}\n"

        f"Endpoint: "
        f"{payload.get('endpoint')}\n"

        f"Diagnóstico: "
        f"{resultado_agente.get('diagnostico')}\n"

        f"Acción tomada: "
        f"{resultado_agente.get('accion_tomada')}\n"

        f"Resumen: "
        f"{resultado_agente.get('resumen_para_humano')}"
    )

    embedding = get_embedding(texto)

    supabase.table("incidentes").insert({

        "content": texto,

        "embedding": embedding,

        "metadata": {

            "error_id":
                payload.get("error_id"),

            "timestamp":
                payload.get("timestamp")
                or datetime.now(
                    timezone.utc
                ).isoformat(),

            "endpoint":
                payload.get("endpoint"),

            "severity":
                payload.get("severity"),

            "origen":
                payload.get("origen"),

            "diagnostico":
                resultado_agente.get(
                    "diagnostico"
                ),

            "accion_tomada":
                resultado_agente.get(
                    "accion_tomada"
                ),

            "confianza":
                resultado_agente.get(
                    "confianza"
                ),

            "archivo_modificado":
                resultado_agente.get(
                    "archivo_modificado"
                ),

            "resumen_para_humano":
                resultado_agente.get(
                    "resumen_para_humano"
                ),

            "estado":
                (
                    "resuelto"
                    if resultado_agente.get(
                        "accion_tomada"
                    ) == "fix_aplicado"
                    else "pendiente"
                )
        }

    }).execute()

trabajos = {}

def ejecutar_agente_en_segundo_plano(
    payload,
    contexto_proyecto,
    incidentes_similares,
    repo_path,
    job_id
):
    try:
        print(
            f"[{job_id}] Iniciando agente...",
            flush=True
        )

        prompt_final = armar_prompt_final(
            payload,
            contexto_proyecto,
            incidentes_similares,
            repo_path
        )

        print(
            f"[{job_id}] Ejecutando OpenCode...",
            flush=True
        )

        resultado_agente = llamar_agente(
            prompt_final,
            repo_path
        )

        print(
            f"[{job_id}] OpenCode terminó.",
            flush=True
        )

        guardar_solucion(
            payload,
            resultado_agente
        )

        print(
            f"[{job_id}] Solución guardada.",
            flush=True
        )

        trabajos[job_id] = {
            "estado": "completado",
            "resultado": resultado_agente
        }

    except Exception as e:

        print(
            f"[{job_id}] ERROR: {e}",
            flush=True
        )

        trabajos[job_id] = {
            "estado": "error",
            "error": str(e)
        }

# ============================================================
# ENDPOINT PRINCIPAL
# ============================================================

@app.route(
    "/procesar",
    methods=["POST"]
)
def procesar():

    if not AGENT_TOKEN:

        return jsonify({
            "error":
                "AGENT_TOKEN no está definido "
                "en el .env del agente"
        }), 500


    if request.headers.get(
        "X-Agent-Token"
    ) != AGENT_TOKEN:

        return jsonify({
            "error": "token inválido"
        }), 401


    payload = request.get_json()


    if not payload:

        return jsonify({
            "error": "body inválido"
        }), 400


    descripcion = (
        payload.get("descripcion")
        or payload.get("stack_trace")
        or payload.get("error")
    )


    if not descripcion:

        return jsonify({
            "error":
                "falta 'descripcion' "
                "o 'stack_trace' en el body"
        }), 400


    # Normalizamos el campo para el resto del sistema.
    payload["descripcion"] = descripcion


    repo_path = os.getenv(
        "REPO_PATH"
    )


    if (
        not repo_path
        or not os.path.isdir(repo_path)
    ):

        return jsonify({

            "error":
                f"REPO_PATH mal configurado "
                f"en el .env del agente: "
                f"{repo_path!r}. "
                f"Es la carpeta local de "
                f"repo-victima en ESTA máquina."

        }), 500


    try:

        previo = incidente_reciente(
            payload.get("endpoint")
        )


        if previo:

            return jsonify({

                "skip":
                    f"ya hay un incidente para "
                    f"{payload.get('endpoint')} "
                    f"en las últimas "
                    f"{VENTANA_DEDUP_HORAS} h",

                "incidente_previo":
                    previo

            }), 200


        print(
            "Generando contexto RAG...",
            flush=True
        )


        contexto_proyecto, incidentes_similares = (
            consultar_rag(
                payload["descripcion"]
            )
        )


        # ----------------------------------------
        # CREAR ID DEL TRABAJO
        # ----------------------------------------

        job_id = str(
            uuid.uuid4()
        )


        trabajos[job_id] = {
            "estado": "procesando"
        }


        # ----------------------------------------
        # LANZAR AGENTE EN SEGUNDO PLANO
        # ----------------------------------------

        hilo = threading.Thread(
            target=ejecutar_agente_en_segundo_plano,
            args=(
                payload,
                contexto_proyecto,
                incidentes_similares,
                repo_path,
                job_id
            ),
            daemon=True
        )

        hilo.start()


        # ----------------------------------------
        # RESPONDER INMEDIATAMENTE
        # ----------------------------------------

        return jsonify({

            "estado": "procesando",

            "job_id": job_id,

            "mensaje":
                "El error fue recibido. "
                "El agente está investigando "
                "el repositorio."

        }), 202


    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


@app.route(
    "/trabajos/<job_id>",
    methods=["GET"]
)
def ver_trabajo(job_id):
    trabajo = trabajos.get(job_id)
    if not trabajo:
        return jsonify({"error": "job_id no encontrado"}), 404
    return jsonify(trabajo)


# ============================================================
# SALUD
# ============================================================

@app.route(
    "/salud",
    methods=["GET"]
)
def salud():

    return jsonify({
        "ok": True,
        "servicio":
            "agente-detective-local"
    })


# ============================================================
# INICIO
# ============================================================

if __name__ == "__main__":

    print(
        "Servidor del agente local "
        "corriendo en "
        "http://localhost:5000"
    )

    print(
        "Exponelo con: "
        "cloudflared tunnel "
        "--url http://localhost:5000"
    )

    print(
        f"Modelo del agente: "
        f"{MODELO_AGENTE}"
    )

    app.run(
        host="0.0.0.0",
        port=5000
    )