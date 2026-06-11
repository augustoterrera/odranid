# ROADMAP — Calidad y evolución del agente sin romper lo conversacional

Objetivo: que cada cambio al agente (prompt, guards, estado) sea verificable contra un set de
conversaciones reales, y que exista un proceso fijo para convertir cada incidente de producción
en un test antes de arreglarlo. El set solo crece; un cambio se acepta solo si el caso nuevo pasa
y todo lo anterior sigue verde.

## Principio rector: dónde vive cada tipo de regla

El anti-spaghetti es este árbol de decisión. Ante un comportamiento a corregir o agregar,
la regla va en UNA capa, decidida así:

1. **¿Es un invariante duro verificable por código?** (links permitidos, no mostrar precios,
   datos de contacto textuales, formato WhatsApp, no prometer retiro) → **guard determinístico
   en código** (`guard_agent_answer` y compañía). Nunca confiar esto al prompt.
2. **¿Es extracción de un dato del mensaje?** (medidas, talles, m², diámetros, slugs de links)
   → **extractor determinístico** (como `extract_requested_m2`, `extract_product_slugs`).
   El LLM puede emitirlo, pero el código lo garantiza.
3. **¿Es comportamiento conversacional?** (cuándo preguntar, cuándo buscar, tono, cómo
   presentar) → **regla GENERAL en la sección del prompt que corresponde**. Prohibido pegar
   ejemplos literales del incidente ("Caviahue", "1.00x10") como regla nueva: si el caso no se
   cubre con una regla general, el fix va a la capa 1 o 2.
4. **¿Es estado entre turnos?** → `chat_memory` es la única autoridad de `missing`/
   `should_search`/`pending_slot` post-intake. El prompt describe; el código decide.

Regla de proceso: **ningún fix de comportamiento se mergea sin su caso en `evals/` que falle
antes del fix y pase después.**

---

## Fase 0 — Higiene y visibilidad (1 día)

- [x] Remover el bloque `DEBUG temporal` del webhook en `app/main.py` (filtra la URL de la DB
      con password en la respuesta HTTP y abre 2 conexiones extra por mensaje).
- [x] Métricas de salud del agente en logs (contadores por turno):
  - líneas descartadas por `guard_agent_answer` (hoy el descarte es invisible),
  - re-runs por `agent_should_search_without_tool_call` y `agent_recommendation_hid_products`,
  - conversaciones que superan `chatwoot_history_limit` (riesgo de "olvido" por re-derivación).
- Salida: saber dónde duele de verdad antes de tocar nada.

## Fase 1 — Harness de evaluación (la base de todo) ✅

> Implementado: `evals/` con 13 casos seed (mensajes reales del JSON de n8n), runner pytest,
> aserciones declarativas, snapshot de 257 productos y `make eval` (~70s, 13/13 verde estable).
> Hallazgos ya documentados en los casos: (a) la regla 9 de intake se viola cuando hay specs
> en el historial — intent='consulta_envio'/'consulta_precio' con known lleno, respuesta
> visible correcta (→ Fase 5); (b) "AFA" aparece en títulos reales de mangueras, en conflicto
> con la prohibición del prompt — `brand_rules` excluye términos citados de títulos (→ Fase 4).

Estructura:

```
evals/
  cases/                  # un YAML por caso
    pisos_recomendacion_gimnasio.yaml
    mangueras_disponibilidad_diametro.yaml
    envio_con_link_producto.yaml
    ...
  conftest.py             # runner pytest (marker "eval")
  assertions.py           # aserciones reutilizables registradas por nombre
  fixtures/catalog_snapshot.json   # catálogo CONGELADO para reproducibilidad
```

Formato de caso (declarativo, sin código):

```yaml
name: recomendacion_gimnasio_sin_medidas
history:
  - {role: user, content: "hola busco piso para gimnasio"}
  - {role: assistant, content: "..."}
message: "no sé qué elegir, recomendame vos"
asserts:
  - presents_product          # la respuesta contiene >=1 producto de la búsqueda
  - no_prices
  - only_allowed_links
  - not_asks: ["ancho"]       # no pide el ancho en modo recomendación
  - tool_called: buscar_productos
```

Decisiones:
- **Aserciones determinísticas**, sin LLM judge: cada asercion es una función chica en
  `assertions.py`, referenciada por nombre desde los YAML. Agregar una asercion nueva = una
  función + docstring.
- **Catálogo congelado**: los evals corren `run_pydantic_agent` con un `search` que lee el
  snapshot fijo (reusa `CatalogSearch` local). Así el resultado no depende del catálogo vivo
  ni de Postgres/Typesense, solo del LLM + prompt + guards.
- **LLM real** (gpt-4.1-mini): es lo que se está validando. Costo estimado: ~30 casos × ~2
  llamadas ≈ centavos por corrida.
- Seed inicial: curar 20–30 conversaciones de `data/fixtures/chatwoot_conversaciones.json`
  (727 reales del agente n8n) cubriendo: flujo pisos completo, recomendación, disponibilidad
  mangueras, link de producto + envío, corrección ("no era eso"), operativos (envío/retiro/
  precio/asesor), calzado, mascotas, multi-mensaje (ráfaga unida).
- Comando único: `make eval` (o `uv run pytest -m eval`).

## Fase 2 — CI en GitHub Actions ✅

> Implementado: `.github/workflows/tests.yml` (unitarios en cada push/PR) y
> `.github/workflows/evals.yml` (evals solo cuando el cambio toca `app/agents|chat|search|
> catalog`, `evals/` o por dispatch manual; resumen en el job summary). El runner reintenta
> cada caso hasta 2 veces: una regresión sistemática falla siempre, el ruido del LLM no.
> **Pendiente manual**: cargar el secret `OPENAI_API_KEY` en GitHub
> (Settings → Secrets and variables → Actions).

- Workflow 1 (cada push/PR): tests unitarios existentes. Sin tokens.
- Workflow 2 (evals): corre cuando cambian rutas sensibles (`app/agents/**`, `app/chat/
  chat_memory.py`, `app/search/**`, `evals/**`) y por dispatch manual. `OPENAI_API_KEY`
  como secret del repo. Resumen de casos fallados en el job summary.
- Branch protection opcional: evals verdes requeridos para mergear a main cuando el PR toca
  el prompt o el agente.

## Fase 3 — Exportador de incidentes y flujo de arreglo ✅

> Implementado: `scripts/export_conversation.py --conversation <id_chatwoot>` genera
> `evals/cases/incident_<id>.yaml` (la ráfaga final de mensajes user queda como `message`,
> la respuesta mala del bot se descarta del history). El flujo de 6 pasos de abajo queda
> como el proceso operativo.

Tooling: `scripts/export_conversation.py --conversation <id_chatwoot>` → lee `chat_messages`
(por tailnet al Postgres del VPS) y genera `evals/cases/incident_<id>.yaml` con el historial
real y un bloque `asserts: []` para completar.

**Flujo fijo ante "el bot respondió mal" (este es el proceso completo):**

1. **Exportar**: `export_conversation.py --conversation 1234` → caso YAML con el diálogo real.
2. **Declarar lo esperado**: editar los `asserts` del caso para expresar qué DEBERÍA haber
   hecho el bot (no qué hizo). Si falta una asercion, se agrega a `assertions.py`.
3. **Rojo**: `make eval` → el caso nuevo falla, el resto sigue verde. Confirma que el caso
   reproduce el problema.
4. **Decidir la capa** con el árbol del principio rector (guard / extractor / prompt general /
   estado). Acá se evita el spaghetti: el incidente NUNCA entra como regla literal al prompt.
5. **Verde total**: el fix hace pasar el caso nuevo SIN romper ninguno anterior. Si rompe
   otro caso, el fix era demasiado específico o entró en conflicto con una regla existente:
   se rediseña la regla general, no se parchea el otro caso para que pase.
6. **Commit atómico**: caso + fix juntos. El historial de `evals/cases/` queda como registro
   de todos los comportamientos garantizados.

## Fase 4 — Refactor del prompt (recién acá, con red de seguridad) ✅ (parcial)

> Hecho: (a) system prompt reordenado — estático primero, CONTEXTO DINAMICO al final —
> para aprovechar el prompt caching de OpenAI; (b) conflicto AFA resuelto: prohibido por
> iniciativa propia, permitido citar el título textual de un producto devuelto; (c) regla 9
> reescrita: en operativos lo innegociable es should_search=false y cero herramientas;
> el intent operativo y el contexto de producto en known están permitidos (continuidad).
> **Pendiente** (cuando el set crezca a 30+ casos): deduplicar "la última medida del
> cliente gana" (4 lugares) y purgar ejemplos literales restantes.

- Deduplicar reglas repetidas (la prioridad "última medida del cliente gana" está en 4 lugares).
- Resolver conflictos explícitos (regla 9 de intake vs `linked_products_from_web`).
- Purgar ejemplos literales de incidentes viejos reemplazándolos por reglas generales
  (cada purga validada por el set).
- Reordenar el system prompt: TODO lo estático primero, `CONTEXTO DINAMICO` al final →
  aprovecha el prompt caching automático de OpenAI (menos latencia y costo por turno).
- Opcional: partir el prompt en secciones por dominio (core + pisos + mangueras + operativo)
  concatenadas en `build_pydantic_system_prompt`, para que un cambio de mangueras no toque
  el archivo de pisos.

## Fase 5 — Unificar la máquina de estados ✅

> Hecho: `build_memory_state` documentado como autoridad única; `recompute_missing_slots`
> tiene rama de modo recomendación (sin `ancho_m`; espesor derivado del uso vía
> `RECOMMENDED_ESPESOR_BY_USE`, salvo diseño concreto elegido); `floor_next_question`
> pregunta el uso cuando es lo único que falta; el invariante "si buscó, should_search=true"
> se fuerza en código en `run_pydantic_agent` (el LLM a veces buscaba devolviendo false).

- Alinear `recompute_missing_slots` con las excepciones del prompt (modo recomendación,
  availability lookup): hoy exige `ancho_m` aunque el prompt diga que no se pide, y puede
  dejar `pending_slot="ancho_m"` que contamina el turno siguiente vía
  `apply_pending_slot_to_message`.
- Definir autoridad única: el recompute decide `missing`/`should_search`; el intake del LLM
  aporta `known`. Documentarlo en el código.
- Casos de eval multi-turno que cubran `pending_slot` (responder un número suelto después
  de una recomendación, etc.).

## Fase 6 — Mejoras guiadas por métricas ✅ (núcleo)

- [x] Guard: renumera la lista tras descartar líneas (sin huecos 1., 3., 5.).
- [x] `preload_linked_products`: extrae slugs también de los últimos 6 mensajes user del
      historial (máx. 3 lookups por turno); un follow-up sin link recupera el producto real.
- [x] Engine de búsqueda cacheado por proceso del worker (`ensure_search_configured`).
- [x] Ráfaga: si llegan mensajes nuevos durante la generación, el turno se descarta y la
      task del mensaje nuevo procesa todo junto (`chatwoot_turn_superseded_by_new_messages`).
- [ ] Re-ejecutar el turno cuando el guard descarta más de N líneas — esperar a que la
      métrica `guard_discarded_answer_lines` muestre si pasa en producción.
- [ ] Guard de cantidades: en conv 811 el bot escribió "Necesitás 4 rollos" para un rollo
      de 6 m² cubriendo 60 m² (real: 10) — el LLM copia/alucina cantidades en vez de usar
      `coverage.rolls_needed`. Verificación determinística posible: en líneas que citan un
      producto + "Necesitás X rollos", comparar X contra el coverage del hit y corregir/
      loggear. Mitigado parcialmente por el sort (los rollos chicos ya no se presentan
      para superficies grandes).
- [ ] Evaluar modelo alternativo con el set como juez (`ODRANID_EVAL_MODEL`).

---

## Criterio de "terminado" de cada fase

| Fase | Listo cuando |
|------|--------------|
| 0 | Debug removido en prod; métricas visibles en logs |
| 1 | `make eval` corre 20+ casos reales en <5 min y está documentado |
| 2 | Evals corren en Actions sobre PRs que tocan el agente |
| 3 | Un incidente real recorrió el flujo completo exportar→rojo→fix→verde |
| 4 | Prompt sin reglas duplicadas ni ejemplos literales; set 100% verde |
| 5 | Una sola autoridad de estado; casos multi-turno verdes |
| 6 | Según métricas; cada mejora entra con su caso de eval |
