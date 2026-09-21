"""
Pipeline del agente — El Detective de Bugs
============================================

Este script hace TODO el trabajo que hacen los nodos de n8n en el
Módulo 3 (Flujo A + Flujo B), pero en un solo script de Python. Sirve
para probar el flujo completo sin depender de que n8n ya esté 100%
armado, y n8n puede llamarlo igual desde un nodo "Execute Command"
cuando esté listo.

Pasos:
  1. Recibe el payload normalizado del error (el mismo formato que ya
     arman los nodos "Normalizar payload" del workflow de n8n).
  2. RAG — consulta ambas colecciones en Supabase:
       - documents_bugs  (contexto del proyecto: los 10+1 chunks fijos)
       - incidentes      (historial de casos ya resueltos)
  3. Modelo simple (gpt-4o-mini) — arma un prompt limpio y acotado
     combinando el error + lo recuperado del RAG. Este paso es barato
     a propósito: no diagnostica nada, solo prepara el input para el
     agente de código, que es el paso caro.
  4. Agente de código (OpenCode / Claude Code) — recibe el prompt y
     hace el diagnóstico real, en modo no interactivo.
  5. Guarda la solución: UNA sola fila en la tabla `incidentes` de
     Supabase, que sirve a la vez como persistencia (Módulo 6) y como
     RAG del historial (Módulo 4) — exactamente como plantea el plan
     original, sin duplicar infraestructura.

Uso:
    python pipeline.py '{"error_id": "...", "descripcion": "...", ...}'

El JSON de entrada es un string (así n8n se lo puede pasar directo desde
un nodo Execute Command con {{ JSON.stringify($json) }}).
"""

import json
import subprocess
import sys
from datetime import datetime, timezone

import os
from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client

load_dotenv()

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY"),
)

MODELO_AGENTE = os.getenv("AGENTE_MODELO", "opencode-go/deepseek-v4-flash")
MODELO_SIMPLE = "gpt-4o-mini"
CANTIDAD_CHUNKS_RAG = 3


# ─────────────────────────────────────────────
# PASO 1 — Embeddings (se reutiliza para RAG y para indexar al final)
# ─────────────────────────────────────────────
def get_embedding(texto: str):
    respuesta = openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=texto,
    )
    return respuesta.data[0].embedding


# ─────────────────────────────────────────────
# PASO 2 — RAG: consulta las dos colecciones
# ─────────────────────────────────────────────
def consultar_rag(descripcion_error: str):
    embedding = get_embedding(descripcion_error)

    contexto_proyecto = supabase.rpc(
        "match_documents_bugs",
        {
            "query_embedding": embedding,
            "match_count": CANTIDAD_CHUNKS_RAG,
            "filter": {},
        },
    ).execute()

    incidentes_similares = supabase.rpc(
        "match_incidentes",
        {
            "query_embedding": embedding,
            "match_count": CANTIDAD_CHUNKS_RAG,
            "filter": {},
        },
    ).execute()

    return contexto_proyecto.data or [], incidentes_similares.data or []


# ─────────────────────────────────────────────
# PASO 3 — Modelo simple: arma el prompt final, barato
# ─────────────────────────────────────────────
def armar_prompt_final(payload: dict, contexto_proyecto: list, incidentes_similares: list) -> str:
    contexto_texto = "\n\n".join(
        f"- {c['content']}" for c in contexto_proyecto
    ) or "Sin contexto de proyecto relevante encontrado."

    incidentes_texto = "\n\n".join(
        f"- {i['content']}" for i in incidentes_similares
    ) or "No hay incidentes similares previos registrados."

    instruccion_para_modelo_simple = f"""
Sos un asistente que arma un briefing corto y claro para un agente de
código que va a diagnosticar un bug. No diagnostiques vos mismo, no
propongas fixes — solo organizá la información en un párrafo denso y
accionable, sin relleno.

Error nuevo:
- Endpoint: {payload.get('endpoint')}
- Severidad: {payload.get('severity')}
- Descripción/stack trace: {payload.get('descripcion')}

Contexto del proyecto relevante (RAG):
{contexto_texto}

Incidentes similares ya resueltos antes (RAG):
{incidentes_texto}

Armá el briefing en español, en un solo párrafo de no más de 120 palabras.
""".strip()

    respuesta = openai_client.chat.completions.create(
        model=MODELO_SIMPLE,
        messages=[{"role": "user", "content": instruccion_para_modelo_simple}],
        temperature=0.2,
    )

    briefing = respuesta.choices[0].message.content.strip()

    prompt_final = f"""
Analizá este error y decidí qué hacer, siguiendo la regla de autonomía
del proyecto: aplicá el fix directamente en todos los casos, pero si la
lógica de negocio es ambigua o discutible, marcá confianza baja y
explicá el riesgo en el resumen para el humano, en vez de escalar sin
resolver.

{briefing}

Repo en: {payload.get('repo_path')}

Devolvé la respuesta ÚNICAMENTE en este formato JSON, sin texto extra:
{{
  "diagnostico": "texto explicando la causa raíz",
  "accion_tomada": "fix_aplicado | solo_diagnostico | escalado",
  "confianza": "alta | media | baja",
  "archivo_modificado": "ruta o null",
  "resumen_para_humano": "1-2 frases"
}}
""".strip()

    return prompt_final


# ─────────────────────────────────────────────
# PASO 4 — Agente de código (OpenCode / Claude Code)
# ─────────────────────────────────────────────
def llamar_agente(prompt_final: str) -> dict:
    comando = [
        "opencode", "run",
        "--agent", "detective-de-bugs",
        "--model", MODELO_AGENTE,
        "--format", "json",
        prompt_final,
    ]

    try:
        resultado = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        print("ERROR: no se encontró el comando 'opencode'. ¿Está instalado y en el PATH?")
        sys.exit(1)

    if resultado.returncode != 0:
        print("El agente devolvió un error:")
        print(resultado.stderr)
        sys.exit(1)

    try:
        return json.loads(resultado.stdout)
    except json.JSONDecodeError:
        print("El agente no devolvió JSON válido. Salida cruda:")
        print(resultado.stdout)
        sys.exit(1)


# ─────────────────────────────────────────────
# PASO 5 — Guardar la solución (persistencia + RAG del historial, unificado)
# ─────────────────────────────────────────────
def guardar_solucion(payload: dict, resultado_agente: dict):
    texto_para_embeber = (
        f"Error: {payload.get('descripcion')}\n"
        f"Endpoint: {payload.get('endpoint')}\n"
        f"Diagnóstico: {resultado_agente.get('diagnostico')}\n"
        f"Acción tomada: {resultado_agente.get('accion_tomada')}\n"
        f"Resumen: {resultado_agente.get('resumen_para_humano')}"
    )

    embedding = get_embedding(texto_para_embeber)

    supabase.table("incidentes").insert({
        "content": texto_para_embeber,
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


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print('Uso: python pipeline.py \'{"descripcion": "...", "endpoint": "...", ...}\'')
        sys.exit(1)

    payload = json.loads(sys.argv[1])

    print(f"1/4 — Consultando RAG para: {payload.get('endpoint')}")
    contexto_proyecto, incidentes_similares = consultar_rag(payload.get("descripcion", ""))
    print(f"     {len(contexto_proyecto)} chunks de contexto, {len(incidentes_similares)} incidentes similares")

    print("2/4 — Armando prompt final con el modelo simple (gpt-4o-mini)")
    prompt_final = armar_prompt_final(payload, contexto_proyecto, incidentes_similares)

    print(f"3/4 — Llamando al agente de código ({MODELO_AGENTE})")
    resultado_agente = llamar_agente(prompt_final)
    print(json.dumps(resultado_agente, indent=2, ensure_ascii=False))

    print("4/4 — Guardando la solución (persistencia + RAG del historial)")
    guardar_solucion(payload, resultado_agente)

    print("\nListo. Incidente procesado y guardado.")


if __name__ == "__main__":
    main()
