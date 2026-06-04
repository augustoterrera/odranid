# Fase 1 — Limpieza del proyecto Odranid

> Brief para ejecutar con Claude Code dentro del repo. Objetivo: dejar el proyecto
> limpio, seguro y reproducible **sin cambiar el comportamiento** del agente, el intake
> ni la búsqueda. Todo lo que toque esa lógica es Fase 2 y se define aparte.

## Reglas para esta fase (leer antes de empezar)

- Trabajá en una rama nueva: `limpieza-fase-1`.
- **Antes de borrar nada**, asegurá un punto de restauración (commit inicial con todo el estado actual).
- Un cambio lógico por commit, con mensaje claro.
- Corré la suite de tests (pytest, ~9 archivos) **antes de empezar y después de cada paso**.
  En esta fase **nada debería romperse**. Si aparece un test en rojo, frená y reportá.
- **NO toques** la lógica de los agentes, el intake ni el scoring de búsqueda.
  Si una limpieza te obligara a tocar esa lógica, no la hagas: marcala como
  `// pendiente Fase 2` y seguí con el resto.
- No actualices versiones de dependencias en esta fase. El objetivo es ordenar y
  reproducir lo que ya hay, no upgradear.

---

## Paso 0 — Punto de partida seguro

- Verificá el estado de git. Si la rama actual (`master`) no tiene commits, hacé `git init`
  si hace falta y un commit inicial: "snapshot inicial antes de limpieza" con todo el árbol.
- Creá y cambiate a la rama `limpieza-fase-1`.
- Corré `pytest` y guardá el resultado de referencia (cuántos pasan hoy).

## Paso 1 — Secretos y .gitignore (lo más urgente)

- Creá/actualizá `.gitignore` con al menos:
  `.env`, `.env.*` (excepto `.env.example`), `.cache/`, `reports/`, `__pycache__/`,
  `*.pyc`, `.venv/`, `*.egg-info/`.
- Sacá `.env` del control de versiones sin borrarlo del disco: `git rm --cached .env`.
  Conservá `.env.example`.
- Listá en el resumen final (NO en texto plano dentro de un commit) qué claves de secreto
  aparecen en `.env`/`.env.example`, para que el dueño las rote: API key de OpenAI, token
  de acceso de Chatwoot, webhook secret de Chatwoot, credenciales de WooCommerce, URL/clave
  de Postgres. **No imprimas los valores**, solo los nombres.

## Paso 2 — Una sola fuente de verdad para las migraciones

- Confirmá cómo se construye la base: el servicio `migrate` del `docker-compose.yml` aplica
  `postgres/migrations/*.sql` con psql. Esa es la fuente de verdad.
- Hacé un `diff` entre cada `supabase/migrations/00X` y su par `postgres/migrations/00X`
  y dejá un resumen del diff en el mensaje de commit (por si había algo solo en supabase).
- Borrá `supabase/migrations/` completo.
- `postgres/schema.sql`: grepeá si algún código o script lo carga. Si solo duplica lo que ya
  está en las migraciones y nadie lo usa, borralo o convertilo en un snapshot claramente
  rotulado como generado. **No lo borres si algo lo referencia.**

## Paso 3 — Documentación desalineada

- En `AGENT.md` y demás `.md`: eliminá toda referencia a Supabase como "base de producción"
  y a `app/supabase_store.py` (no existe). El proyecto es Postgres directo + pgvector.
- Verificá que los comandos de arranque y las variables documentadas coincidan con el código real.

## Paso 4 — Código muerto en `app/agent.py`

- **Solo borrar funciones provablemente sin uso. NO mover ni renombrar las que siguen vivas**
  (eso es Fase 2, cuando se reescribe el agente).
- Grepeá cada símbolo de `app/agent.py` en todo el repo.
  - Sin ninguna referencia externa (esperadas: `response_from_search_response`, `format_hit`,
    `search_intro`, y cualquier otra que el grep confirme sin uso) → borralas.
  - En uso (`build_system_prompt`, `clamp_int`, `compact_search_response`) → dejalas donde están.
- Quitá el docstring "DEPRECATED / se elimina en Fase 3" que ya no es verdad.
- Corré tests.

## Paso 5 — Romper el acoplamiento entre los dos motores de búsqueda

- `retrieval.py` importa `post_filter_specific_terms` desde `db_search.py`. Mové esa función
  (y cualquier helper realmente compartido) a un módulo neutral, p. ej. `app/search_common.py`,
  y que **ambos** importen desde ahí.
- **No cambies la lógica**, solo la ubicación de las funciones compartidas.
- La decisión de conservar o eliminar `retrieval.py` como fallback queda para Fase 2.
- Corré tests.

## Paso 6 — Sacar el tooling de experimentación del runtime

- `reports/` (salidas de shadow/replay `.jsonl`/`.md`/`.json`): que quede en `.gitignore` y
  fuera de la imagen de deploy. Si querés conservar reportes, que vayan a una carpeta ignorada.
- `scripts/replay_chatwoot_conversations.py` y `scripts/analyze_shadow_report.py` son
  herramientas de evaluación: dejalos en `scripts/`, pero revisá qué copia el `Dockerfile` y
  asegurate de que no entren al runtime productivo si no se usan ahí.

## Paso 7 — Conexiones a Postgres por operación

- En `app/db_search.py`, `DatabaseCatalogSearch` abre una conexión psycopg nueva en cada método
  (`search`, `count_products`, `catalog_facets`). Hacé que use el connection pool, con el mismo
  patrón que `ChatMemoryStore`.
- Esto **no cambia resultados**, solo el manejo de conexiones.
- Si para lograrlo tuvieras que cambiar la firma de la búsqueda que consume el agente, frená y
  marcalo `pendiente Fase 2`.
- Corré tests y una búsqueda de humo manual si tenés un endpoint para eso.

## Paso 8 — Dependencias reproducibles

- Migrá de `requirements.txt` (rangos `>=`) a `pyproject.toml` + lockfile. Preferido: **uv**
  (alternativa: poetry).
- **Fijá las versiones exactas que están instaladas hoy** (no actualices nada).
- Actualizá el `Dockerfile` para instalar desde el lock.
- Levantá con `docker-compose` y verificá que arranca igual que antes.

---

## Cierre de la fase

- Mergeá `limpieza-fase-1` (o dejá el PR listo para revisión).
- Entregá un resumen con:
  - Qué se borró y qué se movió.
  - El diff encontrado entre `supabase/migrations` y `postgres/migrations`.
  - La lista de **nombres** de secretos a rotar (sin valores).
  - Si algún test o el arranque con compose necesitó atención.
- **No avances con el cambio de agente.** Eso es Fase 2 y se define por separado.