from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "odranid-catalog-service"
    catalog_file: Path = Path("productos.json")
    agent_prompt_file: Path = Path("prompt_agente_odranid.md")
    context_cache_file: Path = Path("/tmp/odranid_catalog_context.txt")
    # TTL del contexto de catálogo. Evita recalcular las facetas de Postgres (~3s)
    # en cada mensaje; se invalida al sincronizar/recargar el catálogo.
    catalog_context_ttl_seconds: int = 300
    woocommerce_base_url: str = "https://odranid.com.ar"
    woocommerce_per_page: int = 100
    woocommerce_max_pages: int = 50
    woocommerce_stock_status: str = "instock,outofstock"
    database_url: str | None = None
    chatwoot_base_url: str | None = None
    chatwoot_account_id: int | None = None
    chatwoot_api_access_token: str | None = None
    chatwoot_webhook_secret: str | None = None
    chatwoot_webhook_timestamp_tolerance_seconds: int = 300
    # En producción poner en True: si no hay webhook secret, el arranque aborta en vez
    # de aceptar cualquier POST sin firmar. En dev local queda False por comodidad.
    require_webhook_secret: bool = False
    chatwoot_auto_reply: bool = True
    chatwoot_agent_limit: int = 5
    chatwoot_history_limit: int = 16
    chat_memory_enabled: bool = True
    chatwoot_lock_seconds: int = 60
    chatwoot_lock_wait_seconds: int = 20
    chatwoot_debounce_seconds: int = 5
    chatwoot_debounce_retry_seconds: int = 3
    chatwoot_job_max_retries: int = 5
    chatwoot_outbox_max_retries: int = 5
    chatwoot_stale_processing_minutes: int = 15
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_timezone: str = "America/Argentina/Tucuman"
    typesense_host: str = "localhost"
    typesense_port: int = 8108
    typesense_protocol: str = "http"
    typesense_api_key: str | None = None
    typesense_collection: str = "catalog_products"
    # When True (and an API key is set), /search routes through Typesense instead
    # of pgvector. Typesense is the chosen search backend; falls back to pgvector
    # automatically when no Typesense API key is configured (e.g. tests).
    typesense_search_enabled: bool = True
    # How often the beat scheduler refreshes the Typesense index from the catalog.
    typesense_sync_minutes: int = 30
    # How often beat ingests WooCommerce into Postgres, including embedding cache refresh.
    catalog_sync_minutes: int = 60

    # Keep provider details outside business logic. These can point to
    # OpenAI, another embedding provider, or a local model later.
    embedding_model: str = "text-embedding-3-small"
    agent_model: str = "gpt-4.1-mini"
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "ODRANID_OPENAI_API_KEY"),
    )
    vector_top_k: int = 50

    # Token para los endpoints /admin/*. Si está vacío, admin queda DESHABILITADO
    # (fail-closed): los endpoints responden 503. En producción es obligatorio.
    admin_api_token: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_prefix="ODRANID_", extra="ignore")


settings = Settings()
