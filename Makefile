.PHONY: test eval eval-snapshot reset-chat

# Borra TODA la memoria conversacional (mensajes, estado, dedupe, outbox) del Postgres
# del compose. Pensado para DEV; pide confirmación porque en el VPS de prod el comando
# sería igual de destructivo.
# Para iterar pruebas sin confirmar cada vez: make reset-chat FORCE=1
reset-chat:
	@if [ "$(FORCE)" != "1" ]; then \
	  read -p "Esto borra TODAS las conversaciones de esta máquina. Escribí 'borrar' para confirmar: " ok; \
	  [ "$$ok" = "borrar" ] || { echo "cancelado"; exit 1; }; \
	fi; \
	docker compose exec postgres psql -U $${POSTGRES_USER:-steel} -d $${POSTGRES_DB:-odranid} -c \
	  "truncate chat_messages, chat_outbox_messages, chat_processed_events, chat_webhook_jobs, chat_conversations cascade;"; \
	docker compose exec dragonfly redis-cli flushall
	@echo "memoria conversacional borrada: la próxima conversación arranca de cero"

# Tests unitarios (sin tokens, sin red).
test:
	uv run pytest -q

# Evals conversacionales: agente REAL contra catálogo congelado. Requiere OPENAI_API_KEY
# (en el entorno o en .env). Modelo configurable: ODRANID_EVAL_MODEL=gpt-4.1-mini
eval:
	uv run pytest evals -q

# Un solo caso: make eval-case CASE=pisos_recomendacion_gimnasio
eval-case:
	uv run pytest evals -q -k $(CASE)

# Regenera el catálogo congelado desde data/productos.json (cuando cambia la normalización).
eval-snapshot:
	uv run python scripts/build_eval_snapshot.py
