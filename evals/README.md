# Evals conversacionales

Set de regresión del agente: cada caso es una conversación con aserciones determinísticas
sobre cómo DEBE responder el bot. Corren el agente real (gastan tokens OpenAI) contra el
catálogo congelado de `fixtures/catalog_snapshot.json` — sin Postgres, sin Typesense, sin
Chatwoot. Una corrida depende solo de: prompt + modelo + guards.

```bash
make eval                                  # todo el set
make eval-case CASE=pisos_recomendacion    # un caso
make eval-snapshot                         # regenerar el catálogo congelado
```

Requiere `OPENAI_API_KEY` (entorno o `.env`). Sin la key los evals se saltean.

## Reglas del set

1. **El set solo crece.** Nunca se borra un caso porque "molesta": si un cambio lo rompe,
   el cambio entra en conflicto con un comportamiento garantizado y se rediseña.
2. **Todo fix de comportamiento entra con su caso**: primero el caso en rojo (reproduce el
   problema), después el fix, commit de ambos juntos.
3. **Dónde va cada fix** (el árbol completo está en `docs/ROADMAP_CALIDAD_AGENTE.md`):
   invariante duro → guard en código · extracción de datos → extractor determinístico ·
   comportamiento conversacional → regla GENERAL en el prompt (nunca el ejemplo literal
   del incidente) · estado entre turnos → `chat_memory`.

## Anatomía de un caso

```yaml
# Comentario: qué comportamiento garantiza este caso y por qué.
history:                          # turnos previos (opcional)
  - {role: user, content: "..."}
  - {role: assistant, content: "..."}
message: "último mensaje"         # \n simula ráfaga de mensajes unidos por el debounce
asserts:
  - tool_called: buscar_productos
  - presents_product
  - not_asks: ["ancho"]
```

Los invariantes globales corren en TODOS los casos sin declararlos: `no_prices`,
`brand_rules` (AFA, IBIRA, "simil goma", "ranurado", "metros lineales", "redondeo")
y `only_allowed_links`.

## Aserciones disponibles

| Asercion | Argumento | Verifica |
|----------|-----------|----------|
| `tool_called` | nombre (default `buscar_productos`) | se llamó esa herramienta |
| `no_tool_calls` | — | no se llamó ninguna herramienta |
| `presents_product` | — | la respuesta contiene ≥1 producto devuelto por la búsqueda |
| `not_presents_product` | — | no presenta productos |
| `intent_null` | — | intake operativo/institucional vacío (intent, known, should_search) |
| `should_search` | bool (default true) | valor de `intake.should_search` |
| `mentions` | término o lista | todos aparecen en la respuesta (sin acentos, case-insensitive) |
| `mentions_any` | lista | al menos uno aparece |
| `not_mentions` | término o lista | ninguno aparece |
| `asks` | término o lista | alguna línea con `?` contiene cada término |
| `not_asks` | término o lista | ninguna línea con `?` los contiene (mencionarlos fuera de preguntas vale) |
| `max_questions` | entero (default 1) | cantidad máxima de `?` en la respuesta |

Para agregar una asercion: función en `assertions.py` con firma
`(ctx: EvalContext, arg) -> str | None` decorada con `@register("nombre")`.

## Origen de los casos

- Seed inicial: escenarios curados de `data/fixtures/chatwoot_conversaciones.json`
  (727 conversaciones reales del agente n8n viejo). Los mensajes de cliente son reales;
  las aserciones expresan el comportamiento del agente NUEVO.
- Incidentes de producción: exportados con el comando de la Fase 3 del roadmap
  (`scripts/export_conversation.py`, pendiente) como `incident_<id>.yaml`.
