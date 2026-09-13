"""
Servidor local del agente — El Detective de Bugs
=================================================

Corre en tu máquina (la misma donde está el repo víctima y OpenCode
instalado). n8n, aunque esté en Railway (la nube), le pega a este
servidor a través de un túnel de ngrok — así el agente sí tiene acceso
real a los archivos del repo para diagnosticar y aplicar fixes.

Uso:
    python agent_server.py
    (en otra terminal) ngrok http 5000

Copiá la URL https que te da ngrok (algo como
https://abcd-1234.ngrok-free.app) y usala en el nodo HTTP Request de
n8n que reemplaza a todo el bloque de RAG + agente + guardado.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from openai import OpenAI
from supabase import create_client

load_dotenv()

app = Flask(__name__)

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

MODELO_AGENTE = os.getenv("AGENTE_MODELO", "anthropic/claude-sonnet-4-6")
MODELO_SIMPLE = "gpt-4o-mini"
CANTIDAD_CHUNKS_RAG = 3


def get_embedding(texto: str):
    r = openai_client.embeddings.create(model="text-embedding-3-small", input=texto)
    return r.data[0].embedding


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


def armar_prompt_final(payload, contexto_proyecto, incidentes_similares, repo_path):
    contexto_texto = "\n\n".join(f"- {c['content']}" for c in contexto_proyecto) or "Sin contexto relevante."
    incidentes_texto = "\n\n".join(f"- {i['content']}" for i in incidentes_similares) or "Sin incidentes previos."

    briefing_input = f"""
Sos un asistente que arma un briefing corto y claro para un agente de
código. No diagnostiques vos mismo, solo organizá la información.

Error nuevo:
- Endpoint: {payload.get('endpoint')}
- Severidad: {payload.get('severity')}
- Descripción/stack trace: {payload.get('descripcion')}

Contexto del proyecto (RAG):
{contexto_texto}

Incidentes similares (RAG):
{incidentes_texto}

Armá el briefing en español, en un párrafo de no más de 120 palabras.
""".strip()

    briefing = openai_client.chat.completions.create(
        model=MODELO_SIMPLE,
        messages=[{"role": "user", "content": briefing_input}],
        temperature=0.2,
    ).choices[0].message.content.strip()

    return f"""
Analizá este error y decidí qué hacer, siguiendo la regla de autonomía
del proyecto: aplicá el fix directamente en todos los casos, pero si la
lógica de negocio es ambigua, marcá confianza baja y explicá el riesgo
en el resumen para el humano.

{briefing}

Repo en: {repo_path}

Devolvé la respuesta ÚNICAMENTE en este formato JSON, sin texto extra:
{{
  "diagnostico": "texto explicando la causa raíz",
  "accion_tomada": "fix_aplicado | solo_diagnostico | escalado",
  "confianza": "alta | media | baja",
  "archivo_modificado": "ruta o null",
  "resumen_para_humano": "1-2 frases"
}}
""".strip()


def git_pull(repo_path: str):
    subprocess.run(["git", "-C", repo_path, "pull"], check=True, capture_output=True, text=True)


def git_commit_y_push(repo_path: str, mensaje: str):
    subprocess.run(["git", "-C", repo_path, "add", "-A"], check=True)
    commit = subprocess.run(
        ["git", "-C", repo_path, "commit", "-m", mensaje],
        capture_output=True, text=True,
    )
    # Si el agente no tocó ningún archivo (solo_diagnostico/escalado),
    # git commit falla con "nothing to commit" — no es un error real.
    if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
        raise RuntimeError(f"git commit falló: {commit.stdout}\n{commit.stderr}")
    if commit.returncode == 0:
        subprocess.run(["git", "-C", repo_path, "push"], check=True, capture_output=True, text=True)


def llamar_agente(prompt_final: str, repo_path: str) -> dict:
    """
    Acá es donde el agente toca de verdad el repo víctima: corre en esta
    misma máquina, con acceso real al sistema de archivos (un clone de
    git de repo-victima, no el deploy de Render), así que puede leer el
    código y aplicar el fix directamente.
    """
    resultado = subprocess.run(
        ["opencode", "run", "--agent", "detective-de-bugs", "--model", MODELO_AGENTE, "--format", "json", prompt_final],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=repo_path,
    )
    if resultado.returncode != 0:
        raise RuntimeError(f"El agente falló: {resultado.stderr}")
    return json.loads(resultado.stdout)


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


@app.route("/procesar", methods=["POST"])
def procesar():
    payload = request.get_json()
    if not payload or "descripcion" not in payload:
        return jsonify({"error": "falta 'descripcion' en el body"}), 400

    repo_path = os.getenv("REPO_PATH")
    if not repo_path or not os.path.isdir(repo_path):
        return jsonify({
            "error": f"REPO_PATH mal configurado en el .env del agente: {repo_path!r}. "
                     "Esto NO viene del payload — es la carpeta local donde vive el clone "
                     "de repo-victima en ESTA máquina, la del agente."
        }), 500

    try:
        git_pull(repo_path)
        contexto_proyecto, incidentes_similares = consultar_rag(payload["descripcion"])
        prompt_final = armar_prompt_final(payload, contexto_proyecto, incidentes_similares, repo_path)
        resultado_agente = llamar_agente(prompt_final, repo_path)

        if resultado_agente.get("accion_tomada") == "fix_aplicado":
            mensaje_commit = f"fix: {resultado_agente.get('diagnostico', 'fix automático del agente')[:72]}"
            git_commit_y_push(repo_path, mensaje_commit)

        guardar_solucion(payload, resultado_agente)
        return jsonify(resultado_agente), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/salud", methods=["GET"])
def salud():
    return jsonify({"ok": True, "servicio": "agente-detective-local"})


if __name__ == "__main__":
    print("Servidor del agente local corriendo en http://localhost:5000")
    print("Exponelo con: ngrok http 5000")
    app.run(host="0.0.0.0", port=5000)
