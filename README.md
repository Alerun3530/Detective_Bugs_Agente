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

5. `REPO_PATH` en el `.env` tiene que apuntar a **ese clone**, no a
   donde corre Render — el agente edita el clone local, hace `git push`,
   y recién ahí Render se entera (por el auto-deploy conectado a GitHub).
   No viene en el payload: es configuración de la máquina del agente.

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
python pipeline.py '{"error_id": "test-001", "descripcion": "TypeError: Cannot read properties of undefined (reading trim)", "endpoint": "/api/usuarios", "severity": "low", "origen": "webhook_excepcion"}'
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

## Servidor local + Cloudflare Tunnel (cómo lo llama n8n)

Como n8n corre en la nube y el repo víctima + OpenCode están en tu
máquina, `agent_server.py` expone el pipeline como `POST /procesar` y se
publica con un **Cloudflare Tunnel** (cuenta gratuita). El workflow
`../orquestador-agente-local.json` ya está armado para pegarle a esa URL.

```bash
python agent_server.py          # escucha en http://localhost:5000
```

### Exponer el servidor con Cloudflare Tunnel

Instalá `cloudflared` (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).

**Opción A — con un dominio en Cloudflare (URL estable, recomendado):**

```bash
cloudflared tunnel login                       # abre el navegador, elegís el dominio
cloudflared tunnel create detective
cloudflared tunnel route dns detective agente.TU_DOMINIO.com
cloudflared tunnel run --url http://localhost:5000 detective
```

La URL queda fija en `https://agente.TU_DOMINIO.com`, así que no hay que
tocar n8n cada vez que reiniciás.

**Opción B — sin dominio (quick tunnel):**

```bash
cloudflared tunnel --url http://localhost:5000
```

Te da una URL aleatoria `https://xxxx.trycloudflare.com` que cambia en
cada arranque: hay que actualizar el nodo `Llamar agente local` de n8n.

### Seguridad

El túnel es público. `/procesar` exige el header `X-Agent-Token` igual a
`AGENT_TOKEN` del `.env`; en n8n definí la variable de entorno
`AGENT_TOKEN` con el mismo valor (el nodo HTTP usa `{{ $env.AGENT_TOKEN }}`).

### Configurar el workflow de n8n

1. Importar `../orquestador-agente-local.json`.
2. En `Llamar agente local (via Cloudflare Tunnel)`: reemplazar la URL por la del túnel + `/procesar`.
3. En `Consultar monitor anomalías`: poner la URL pública de Render del repo víctima.
4. Definir `AGENT_TOKEN` en las variables de entorno de n8n.

### Alternativa: `pipeline.py` por Execute Command

Si n8n corre en la misma máquina que el repo, se puede usar un nodo
Execute Command en vez del HTTP Request:

```
python C:\ruta\a\Detective_Bugs_Agente\pipeline.py "{{ JSON.stringify($json) }}"
```
