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

- [ ] Remover el bloque `DEBUG temporal` del webhook en `app/main.py` (filtra la URL de la DB
      con password en la respuesta HTTP y abre 2 conexiones extra por mensaje).
- [ ] Métricas de salud del agente en logs (contadores por turno):
  - líneas descartadas por `guard_agent_answer` (hoy el descarte es invisible),
  - re-runs por `agent_should_search_without_tool_call` y `agent_recommendation_hid_products`,
  - conversaciones que superan `chatwoot_history_limit` (riesgo de "olvido" por re-derivación).
- Salida: saber dónde duele de verdad antes de tocar nada.

## Fase 1 — Harness de evaluación (la base de todo)

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

## Fase 2 — CI en GitHub Actions

- Workflow 1 (cada push/PR): tests unitarios existentes. Sin tokens.
- Workflow 2 (evals): corre cuando cambian rutas sensibles (`app/agents/**`, `app/chat/
  chat_memory.py`, `app/search/**`, `evals/**`) y por dispatch manual. `OPENAI_API_KEY`
  como secret del repo. Resumen de casos fallados en el job summary.
- Branch protection opcional: evals verdes requeridos para mergear a main cuando el PR toca
  el prompt o el agente.

## Fase 3 — Exportador de incidentes y flujo de arreglo

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

## Fase 4 — Refactor del prompt (recién acá, con red de seguridad)

- Deduplicar reglas repetidas (la prioridad "última medida del cliente gana" está en 4 lugares).
- Resolver conflictos explícitos (regla 9 de intake vs `linked_products_from_web`).
- Purgar ejemplos literales de incidentes viejos reemplazándolos por reglas generales
  (cada purga validada por el set).
- Reordenar el system prompt: TODO lo estático primero, `CONTEXTO DINAMICO` al final →
  aprovecha el prompt caching automático de OpenAI (menos latencia y costo por turno).
- Opcional: partir el prompt en secciones por dominio (core + pisos + mangueras + operativo)
  concatenadas en `build_pydantic_system_prompt`, para que un cambio de mangueras no toque
  el archivo de pisos.

## Fase 5 — Unificar la máquina de estados

- Alinear `recompute_missing_slots` con las excepciones del prompt (modo recomendación,
  availability lookup): hoy exige `ancho_m` aunque el prompt diga que no se pide, y puede
  dejar `pending_slot="ancho_m"` que contamina el turno siguiente vía
  `apply_pending_slot_to_message`.
- Definir autoridad única: el recompute decide `missing`/`should_search`; el intake del LLM
  aporta `known`. Documentarlo en el código.
- Casos de eval multi-turno que cubran `pending_slot` (responder un número suelto después
  de una recomendación, etc.).

## Fase 6 — Mejoras guiadas por métricas (orden según lo que diga Fase 0)

- Guard de respuesta: renumerar la lista tras descartar líneas; si descartó más de N líneas,
  re-ejecutar el turno en vez de mandar una respuesta mutilada.
- `preload_linked_products`: extraer slugs también de los últimos mensajes user del historial
  (hoy solo del mensaje actual).
- Cachear el engine de búsqueda en el worker (hoy `configure_search()` reconstruye todo por
  mensaje).
- Mensajes en ráfaga: tras generar la respuesta, verificar si llegaron pendientes nuevos y
  reprocesar todo junto en vez de mandar una respuesta ya obsoleta.
- Evaluar modelo: con el set como juez, probar alternativas de modelo/costo y decidir con
  datos.

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
