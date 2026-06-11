from __future__ import annotations

import hmac
import json
import logging
import re
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .agents.catalog_helpers import AgentError
from .catalog.catalog_context import CatalogContextCache, TTLStringCache
from .chat.chat_memory import (
    ChatMemoryError,
    ChatMemoryStore,
    build_chat_memory_store_from_settings,
)
from .chat.chatwoot import (
    ChatwootError,
    extract_message_event,
    parse_chatwoot_payload,
    verify_chatwoot_signature,
    verify_chatwoot_webhook_token,
)
from .chat.chatwoot_service import chatwoot_event_key, persist_incoming_chatwoot_event
from .core.config import settings
from .catalog.coverage import enrich_search_response
from .search.db_search import DatabaseCatalogSearch, DatabaseSearchError
from .search.embeddings import OpenAIEmbeddingClient
from .core.models import (
    AgentRequest,
    AgentResponse,
    ChatwootWebhookResponse,
    ProductDocument,
    ProductIntakeRequest,
    ProductIntakeResponse,
    SearchRequest,
    SearchResponse,
)
from .catalog.normalization import extract_woocommerce_products, normalize_product
from .search.rag_precontext import build_rag_precontext
from .search.retrieval import CatalogSearch
from .search.typesense_client import build_typesense_client
from .search.typesense_search import TypesenseCatalogSearch
from .catalog.woocommerce import build_client_from_settings

app = FastAPI(title=settings.app_name)
logger = logging.getLogger(__name__)

catalog: list[ProductDocument] = []
search_engine: CatalogSearch | None = None
db_search_engine: DatabaseCatalogSearch | None = None
typesense_search_engine: TypesenseCatalogSearch | None = None
context_cache = CatalogContextCache(settings.context_cache_file)
# Cachea el string del contexto de catálogo con TTL para no pegarle a Postgres
# (facetas, ~3s) en cada mensaje. Se invalida al recargar/sincronizar el catálogo.
catalog_context_ttl = TTLStringCache(settings.catalog_context_ttl_seconds)
chat_memory_store: ChatMemoryStore | None = None


@app.on_event("startup")
def startup() -> None:
    enforce_webhook_secret_policy()
    configure_search()
    configure_chat_memory()


def enforce_webhook_secret_policy() -> None:
    """Producción no debería aceptar webhooks sin firmar. Si se exige el secret y
    falta, abortar el arranque; si no se exige pero falta, avisar fuerte."""
    if settings.chatwoot_webhook_secret:
        return
    if settings.require_webhook_secret:
        raise RuntimeError(
            "ODRANID_REQUIRE_WEBHOOK_SECRET=true pero falta ODRANID_CHATWOOT_WEBHOOK_SECRET: "
            "el webhook aceptaría cualquier POST sin firmar. Abortando arranque inseguro."
        )
    logger.warning(
        "Webhook de Chatwoot SIN secret configurado: acepta cualquier POST (inseguro para "
        "producción). Configurá ODRANID_CHATWOOT_WEBHOOK_SECRET."
    )


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    """Guard de los endpoints /admin/*. Fail-closed: sin token configurado, admin
    queda deshabilitado (503); token ausente o incorrecto → 401."""
    token = settings.admin_api_token
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Admin deshabilitado: configurá ODRANID_ADMIN_API_TOKEN para usar /admin/*.",
        )
    if not x_admin_token or not hmac.compare_digest(x_admin_token, token):
        raise HTTPException(status_code=401, detail="Token admin inválido o ausente.")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "products": db_search_engine.count_products() if db_search_engine else len(catalog),
        "search_backend": search_backend_name(),
    }


def search_backend_name() -> str:
    if typesense_search_engine is not None:
        return "typesense"
    if db_search_engine is not None:
        return "database"
    return "local"


@app.get("/catalog/context")
def catalog_context() -> dict[str, str]:
    return {"system_context": current_catalog_context()}


@app.post("/intake/analyze", response_model=ProductIntakeResponse)
def intake_analyze(request: ProductIntakeRequest) -> ProductIntakeResponse:
    return get_product_intake(request.query, request.history)


@app.post("/agent/respond", response_model=AgentResponse)
def agent_respond(request: AgentRequest) -> AgentResponse:
    return run_agent(request)


@app.get("/webhooks/chatwoot/health")
def chatwoot_webhook_health() -> dict[str, object]:
    return {
        "ok": True,
        "endpoint": "/webhooks/chatwoot",
        "auto_reply": settings.chatwoot_auto_reply,
        "has_base_url": bool(settings.chatwoot_base_url),
        "has_account_id": settings.chatwoot_account_id is not None,
        "has_api_access_token": bool(settings.chatwoot_api_access_token),
        "has_webhook_secret": bool(settings.chatwoot_webhook_secret),
        "has_openai": bool(settings.openai_api_key),
        "memory_enabled": settings.chat_memory_enabled,
        "has_memory_store": chat_memory_store is not None,
        "history_limit": settings.chatwoot_history_limit,
        "lock_seconds": settings.chatwoot_lock_seconds,
        "search_backend": "database" if db_search_engine else "local",
    }


@app.post("/webhooks/chatwoot", response_model=ChatwootWebhookResponse)
async def chatwoot_webhook(request: Request) -> ChatwootWebhookResponse:
    raw_body = await request.body()
    is_verified = verify_chatwoot_signature(
        raw_body=raw_body,
        secret=settings.chatwoot_webhook_secret,
        signature=request.headers.get("x-chatwoot-signature"),
        timestamp=request.headers.get("x-chatwoot-timestamp"),
        tolerance_seconds=settings.chatwoot_webhook_timestamp_tolerance_seconds,
    )
    if not is_verified:
        is_verified = verify_chatwoot_webhook_token(
            secret=settings.chatwoot_webhook_secret,
            token=request.query_params.get("token"),
        )
    if not is_verified:
        raise HTTPException(status_code=401, detail="Invalid Chatwoot webhook signature")

    try:
        payload = parse_chatwoot_payload(raw_body)
        event, ignore_reason = extract_message_event(payload, settings.chatwoot_history_limit)
    except ChatwootError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if event is None:
        return ChatwootWebhookResponse(
            ok=True,
            handled=False,
            reason=ignore_reason,
            event=str(payload.get("event") or ""),
            message_id=payload.get("id"),
        )

    if chat_memory_store is None:
        raise HTTPException(status_code=503, detail="Chat memory store is required for Chatwoot webhooks")

    event_key = chatwoot_event_key(
        {key.lower(): value for key, value in request.headers.items()},
        event.conversation_id,
        event.message_id,
    )
    try:
        # El store usa el ConnectionPool SINCRÓNICO de psycopg, que no se comporta bien
        # invocado directo dentro del event loop async (las escrituras no persistían).
        # Lo corremos en un threadpool: contexto sync real donde el pool commitea OK.
        is_new, conversation, job_id = await run_in_threadpool(
            persist_incoming_chatwoot_event, chat_memory_store, event_key, event, payload
        )
    except ChatMemoryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not is_new:
        return ChatwootWebhookResponse(
            ok=True,
            handled=False,
            status="duplicate",
            reason="duplicate_event",
            event=event.event,
            message_id=event.message_id,
            conversation_id=event.conversation_id,
        )

    from .tasks.chatwoot_tasks import process_chatwoot_conversation, set_conversation_debounce

    set_conversation_debounce(conversation.id)
    reason = "queued_for_celery_processing"
    try:
        process_chatwoot_conversation.apply_async(
            (str(conversation.id),),
            queue="chatwoot_messages",
            countdown=settings.chatwoot_debounce_seconds,
        )
    except Exception as exc:
        reason = "queued_in_db_celery_dispatch_failed"
        logger.exception(
            "chatwoot_celery_enqueue_failed",
            extra={"event_key": event_key, "conversation_id": conversation.id, "job_id": job_id, "error": str(exc)},
        )
    return ChatwootWebhookResponse(
        ok=True,
        handled=True,
        status="queued",
        reason=reason,
        event=event.event,
        message_id=event.message_id,
        conversation_id=event.conversation_id,
        job_id=job_id,
    )


def run_agent(request: AgentRequest) -> AgentResponse:
    """LLM-only pipeline: every message goes through the Agno team.

    There is no deterministic keyword interception or fallback. The
    RequirementsAgent classifies intent and the CatalogAgent answers
    (institutional/conversational from its prompt, or product search).
    """
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY is required for the agent")
    return run_openai_agent(request)


def run_openai_agent(request: AgentRequest) -> AgentResponse:
    from .agents.pydantic_agent import run_pydantic_agent

    try:
        return run_pydantic_agent(
            request=request,
            search=perform_search,
            api_key=str(settings.openai_api_key),
            catalog_context=current_catalog_context(),
            model=settings.agent_model,
            prompt_file=settings.agent_prompt_file,
        )
    except (AgentError, DatabaseSearchError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _build_context_with_intake(request: AgentRequest, intake: ProductIntakeResponse) -> str:
    """Build catalog context using the LLM-extracted intake, not the deterministic one."""
    search_query = build_search_query(intake, request)
    return "\n\n".join(
        [
            current_catalog_context(),
            build_rag_precontext(
                request=request,
                search_query=search_query,
                intake=intake,
            ),
        ]
    )


def current_catalog_context_for_request(request: AgentRequest) -> str:
    intake = get_product_intake(request.message, request.history)
    return _build_context_with_intake(request, intake)


def current_agent_context(request: AgentRequest) -> str:
    return current_catalog_context_for_request(request)


def get_product_intake(query: str, history: list[AgentMessage]) -> ProductIntakeResponse:
    """Analyze product intake through the single PydanticAI agent."""
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY is required for intake analysis")
    response = run_openai_agent(AgentRequest(message=query, history=history, limit=1))
    return response.intake or ProductIntakeResponse()


def build_search_query(intake: ProductIntakeResponse, request: AgentRequest) -> str:
    """Build the pre-search query from the LLM-extracted state.

    The LLM-extracted known dict is clean (corrections already applied), so we
    build the query directly from it. When the intake carries no rubro, fall
    back to the raw conversation text (no keyword parsing).
    """
    if intake.known and intake.known.get("rubro"):
        from .chat.chat_memory import known_to_natural_text
        base = known_to_natural_text(intake.known)
        if base:
            return clean_agent_search_query(base)
    return search_query_from_agent_request(request)


def search_query_from_agent_request(request: AgentRequest) -> str:
    """Build a raw search query from the conversation text, no keyword parsing."""
    user_messages = [message.content for message in request.history if message.role == "user"]
    structured_context = [message for message in user_messages if message.startswith("Datos ya recopilados:")]
    if structured_context:
        query = f"{structured_context[-1]} {request.message}"
    else:
        parts = [*user_messages[-4:], request.message]
        query = " ".join(part.strip() for part in parts if part.strip())
    return clean_agent_search_query(query or request.message)


def clean_agent_search_query(query: str) -> str:
    query = re.sub(r"https?://\S+", " ", query)
    query = re.sub(r"\*?Odranid\*?!?", "Odranid", query, flags=re.IGNORECASE)
    query = re.sub(r"\bHola\s+Odranid\b", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\bVengo de la tienda online\b", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\by quisiera saber\b", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\s+", " ", query).strip()
    return query


def configure_chat_memory() -> None:
    global chat_memory_store
    if not settings.chat_memory_enabled:
        chat_memory_store = None
        return
    chat_memory_store = build_chat_memory_store_from_settings(settings)


@app.post("/search", response_model=SearchResponse)
def search(request: SearchRequest) -> SearchResponse:
    try:
        return perform_search(request)
    except DatabaseSearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def current_catalog_context() -> str:
    return catalog_context_ttl.get(_load_catalog_context)


def _load_catalog_context() -> str:
    if db_search_engine is not None:
        try:
            return db_search_engine.catalog_context()
        except DatabaseSearchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return context_cache.get(catalog)


def perform_search(request: SearchRequest) -> SearchResponse:
    # Product links from the web should resolve against Postgres, the source of
    # truth, even when Typesense is enabled. Typesense keeps `link` unindexed, so
    # exact URL lookups must bypass hybrid search.
    if request.filters.product_slug:
        if db_search_engine is not None:
            return enrich_search_response(db_search_engine.search(request))
        if search_engine is not None:
            return enrich_search_response(search_engine.search(request))

    if typesense_search_engine is not None:
        return enrich_search_response(typesense_search_engine.search(request))

    if db_search_engine is not None:
        return enrich_search_response(db_search_engine.search(request))

    if search_engine is None:
        raise HTTPException(status_code=503, detail="Catalog not loaded")
    return enrich_search_response(search_engine.search(request))


@app.post("/admin/reload", dependencies=[Depends(require_admin_token)])
def reload_catalog() -> dict[str, object]:
    configure_search(force_local_reload=True)
    catalog_context_ttl.invalidate()
    return health()


@app.post("/admin/fetch-woocommerce", dependencies=[Depends(require_admin_token)])
def fetch_woocommerce() -> dict[str, object]:
    client = build_client_from_settings(settings)
    raw_products = client.fetch_products()
    load_raw_products(raw_products)
    return {"ok": True, "products": len(catalog)}


@app.post("/admin/typesense-sync", dependencies=[Depends(require_admin_token)])
def typesense_sync() -> dict[str, object]:
    """Encola un rebuild completo del índice de Typesense en el worker de catálogo.
    Async para no bloquear el request (un sync completo puede tardar minutos). El
    contexto cacheado se refresca solo por TTL una vez que la task termina."""
    from .tasks.catalog_tasks import sync_typesense_catalog

    async_result = sync_typesense_catalog.delay(recreate=True)
    return {"ok": True, "status": "queued", "task": "sync_typesense_catalog", "task_id": async_result.id}


@app.post("/admin/sync-catalog", dependencies=[Depends(require_admin_token)])
def sync_catalog() -> dict[str, object]:
    """Encola el sync WooCommerce -> Postgres en el worker de catálogo. Async: el
    fetch + embeddings + upsert puede tardar minutos; devolvemos el task_id para
    seguirlo (logs/Flower). El contexto cacheado se refresca solo por TTL."""
    from .tasks.catalog_tasks import sync_catalog_to_postgres

    async_result = sync_catalog_to_postgres.delay()
    return {"ok": True, "status": "queued", "task": "sync_catalog_to_postgres", "task_id": async_result.id}


@app.get("/admin/retargeting-stats", dependencies=[Depends(require_admin_token)])
def retargeting_stats() -> dict[str, object]:
    """Funnel de retargeting: enviados, reactivados (el cliente escribió después
    del recordatorio) y tasa de reactivación."""
    store = build_chat_memory_store_from_settings(settings)
    if store is None:
        raise HTTPException(status_code=503, detail="Chat memory store is not configured")
    stats = store.retargeting_stats()
    rate = round(stats["reactivated"] / stats["sent"], 4) if stats["sent"] else 0.0
    return {"ok": True, **stats, "reactivation_rate": rate}


def ensure_search_configured() -> None:
    """Configura la búsqueda solo si el proceso aún no la tiene (workers Celery la
    reusan entre tasks; DatabaseCatalogSearch consulta Postgres en vivo, así que
    reutilizar el engine no sirve datos viejos)."""
    if typesense_search_engine is None and db_search_engine is None and search_engine is None:
        configure_search()


def configure_search(force_local_reload: bool = False) -> None:
    global db_search_engine, typesense_search_engine
    typesense_search_engine = build_typesense_engine()
    if not force_local_reload and settings.openai_api_key and settings.database_url:
        db_search_engine = DatabaseCatalogSearch(
            embedder=OpenAIEmbeddingClient(settings.openai_api_key, settings.embedding_model),
            postgres_url=settings.database_url,
        )
        return

    db_search_engine = None
    load_catalog(settings.catalog_file)


def build_typesense_engine() -> TypesenseCatalogSearch | None:
    """Opt-in Typesense search engine. Returns None unless explicitly enabled.

    Postgres stays the source of truth; this only changes where /search reads.
    """
    if not (settings.typesense_search_enabled and settings.typesense_api_key):
        return None
    embedder = (
        OpenAIEmbeddingClient(settings.openai_api_key, settings.embedding_model)
        if settings.openai_api_key
        else None
    )
    return TypesenseCatalogSearch(
        client=build_typesense_client(),
        embedder=embedder,
        collection=settings.typesense_collection,
    )


def load_catalog(path: Path | None = None) -> None:
    if path is not None and path.exists():
        load_catalog_file(path)
        return

    client = build_client_from_settings(settings)
    load_raw_products(client.fetch_products())


def load_catalog_file(path: Path) -> None:
    global catalog, search_engine
    if not path.exists():
        catalog = []
        search_engine = CatalogSearch(catalog)
        return

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_products = extract_woocommerce_products(payload)
    load_raw_products(raw_products)


def load_raw_products(raw_products: list[dict]) -> None:
    global catalog, search_engine
    normalized = []
    for product in raw_products:
        try:
            normalized.append(normalize_product(product))
        except Exception as exc:
            product_id = product.get("id") or product.get("name") or "unknown"
            raise RuntimeError(f"Could not normalize product {product_id}: {exc}") from exc

    catalog = normalized
    search_engine = CatalogSearch(catalog)
    context_cache.invalidate()
    catalog_context_ttl.invalidate()
