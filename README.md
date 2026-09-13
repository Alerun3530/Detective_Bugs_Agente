# Pipeline del agente — El Detective de Bugs

Este script hace de punta a punta lo que hacen los nodos de n8n del
Módulo 3 (Flujo A: consulta RAG + arma prompt + dispara agente — y
Flujo B: parsea resultado + guarda + indexa), en un solo comando de
Python. Sirve para:

- Probar el flujo completo **sin depender de que n8n ya esté armado**.
- Que n8n lo llame directo desde un nodo **Execute Command** una vez que
  esté listo, en vez de tener 6-7 nodos separados.

## Requisitos previos

1. Haber corrido `setup.sql` del RAG (`rag-supabase/setup.sql`) — este
   script usa las tablas `documents_bugs` e `incidentes` que crea ese SQL.
2. Haber corrido `ingest_chunks.py` al menos una vez (si no, el RAG del
   contexto del proyecto está vacío y el paso 1 no va a encontrar nada).
3. Tener `opencode` instalado y accesible desde la terminal (`opencode --version`).
4. **`repo-victima` tiene que estar en un repositorio de GitHub**, y
   necesitás un **clone local separado** de ese repo (no el mismo folder
   que usás para desarrollar a mano, para evitar pisarte cosas):

   ```bash
   git clone https://github.com/tu-usuario/repo-victima.git C:/ruta/repo-victima-clone
   ```

   Y configurar credenciales para que `git push` funcione sin pedir
   contraseña cada vez: lo más simple es un
   [Personal Access Token de GitHub](https://github.com/settings/tokens)
   usado como password una vez (git lo cachea), o una clave SSH.

5. El campo `repo_path` que le llega al pipeline (desde n8n o desde la
   prueba manual) tiene que apuntar a **ese clone**, no a donde corre
   Render — el agente edita el clone local, hace `git push`, y recién
   ahí Render se entera (por el auto-deploy conectado a GitHub).

## Instalación

```bash
cd agente-detective
python -m venv venv
.\venv\Scripts\Activate.ps1      # Windows / PowerShell
pip install -r requirements.txt
```

Copiá `.env.example` a `.env` y completá las credenciales (las mismas
de Supabase y OpenAI que ya usaste para el RAG).

## Uso manual (para probar sin n8n)

```bash
python pipeline.py '{"error_id": "test-001", "descripcion": "TypeError: Cannot read properties of undefined (reading trim)", "endpoint": "/api/usuarios", "severity": "low", "repo_path": "C:/ruta/a/repo-victima", "origen": "webhook_excepcion"}'
```

Con los 3 bugs reales del repo víctima, algunos ejemplos de `descripcion`
para probar cada caso:

| Bug | descripcion de prueba |
|---|---|
| 1 | `ReferenceError: usuarioo is not defined` |
| 2 | `TypeError: Cannot read properties of undefined (reading 'trim')` |
| 3 | El texto que devuelve `/api/monitoreo/anomalias` en el campo `descripcion` de la alerta |

## Qué hace, paso a paso

1. **Git pull** — actualiza el clone local a la última versión antes de tocar nada (por si hubo commits manuales entre medio).
2. **Consulta RAG** — busca los 3 chunks más parecidos en el contexto
   del proyecto (`documents_bugs`) y los 3 incidentes más parecidos ya
   resueltos antes (`incidentes`).
3. **Modelo simple (`gpt-4o-mini`)** — arma un briefing corto combinando
   el error nuevo con lo que trajo el RAG. Este paso es intencionalmente
   barato: no diagnostica nada, solo prepara el input.
4. **Agente de código** — corre `opencode run` con `cwd` apuntando al
   clone local, en modo no interactivo, y espera el JSON de vuelta
   (`diagnostico`, `accion_tomada`, `confianza`, `archivo_modificado`,
   `resumen_para_humano`).
5. **Git commit + push** — solo si `accion_tomada` fue `fix_aplicado`.
   Esto es lo que dispara el auto-deploy de Render.
6. **Guardar la solución** — inserta UNA fila en la tabla `incidentes`
   de Supabase, con el texto embebido y todos los datos en `metadata`.

## Cómo lo llama n8n cuando esté listo

En el nodo `Llamar agente (OpenCode/Claude Code)` del workflow que ya
armamos, se puede reemplazar el comando de `opencode run` directo por:

```
python C:\ruta\a\agente-detective\pipeline.py "{{ JSON.stringify($json) }}"
```

Así n8n solo dispara este script una vez y el script se encarga de todo
el resto del flujo (RAG, modelo simple, agente, guardado) — no hace
falta armar 6 nodos separados dentro de n8n si no querés.
