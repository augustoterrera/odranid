from __future__ import annotations

from typing import Any
from typing import Literal

from pydantic import BaseModel, Field


class ProductSpecs(BaseModel):
    espesor_mm: float | None = None
    ancho_m: float | None = None
    largo_m: float | None = None
    rendimiento_m2: float | None = None
    diametro_mm: float | None = None
    largo_manguera_m: float | None = None


class ProductDocument(BaseModel):
    id: int
    title: str
    slug: str | None = None
    link: str | None = None
    image: str | None = None
    price: float | None = None
    currency: str = "ARS"
    in_stock: bool = True
    stock_text: str | None = None

    rubro: str = "general"
    category: str = "general"
    subcategory: str | None = None
    product_type: str = "unidad"

    floor_kind: str | None = None
    floor_design: str | None = None
    material: str | None = None
    color: str | None = None
    environments: str | None = None
    brands: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    woo_tags: list[str] = Field(default_factory=list)
    technical_tags: list[str] = Field(default_factory=list)
    specs: ProductSpecs = Field(default_factory=ProductSpecs)
    raw_attributes: dict[str, Any] = Field(default_factory=dict)

    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProductFilters(BaseModel):
    # Internal exact lookup used when Chatwoot sends an Odranid product URL.
    # Search tools do not expose this to the LLM.
    product_slug: str | None = None
    rubro: str | None = None
    category: str | None = None
    subcategory: str | None = None
    floor_kind: str | None = None
    floor_design: str | None = None
    material: str | None = None
    color: str | None = None
    espesor_mm: float | None = None
    ancho_m: float | None = None
    talle: int | None = None
    tags: list[str] = Field(default_factory=list)
    in_stock_only: bool = True
    # When True, excludes pisos_vinilicos from results unless category was explicitly requested.
    # Applied as a Python post-filter so no SQL migration is needed.
    exclude_vinilico: bool = False


class SearchRequest(BaseModel):
    query: str
    filters: ProductFilters = Field(default_factory=ProductFilters)
    limit: int = Field(default=10, ge=1, le=50)
    relax_filters: bool = True


class CoverageCalculation(BaseModel):
    requested_m2: float
    sale_unit: str
    coverage_m2: float | None = None
    coverage_source: str | None = None
    rolls_needed: int | None = None
    linear_meters_needed: float | None = None
    quantity_m2: float | None = None
    surplus_m2: float | None = None
    needs_advisor: bool = False
    message: str


class SearchHit(BaseModel):
    product: ProductDocument
    score: float
    matched_filters: list[str] = Field(default_factory=list)
    relaxed_filters: list[str] = Field(default_factory=list)
    coverage: CoverageCalculation | None = None
    # True when the product does not match every requested attribute and is
    # offered as a similar alternative rather than an exact match.
    is_alternative: bool = False


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
    used_relaxation: bool = False
    total_catalog_size: int
    requested_m2: float | None = None


class AgentMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ProductIntakeRequest(BaseModel):
    query: str
    history: list[AgentMessage] = Field(default_factory=list)


class ProductIntakeResponse(BaseModel):
    intent: str | None = None
    known: dict[str, Any] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    should_search: bool = False
    next_question: str | None = None
    confidence: float = Field(default=0, ge=0, le=1)


class AgentRequest(BaseModel):
    message: str
    history: list[AgentMessage] = Field(default_factory=list)
    limit: int = Field(default=5, ge=1, le=10)


class AgentToolTrace(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_count: int = 0


class AgentResponse(BaseModel):
    answer: str
    tool_calls: list[AgentToolTrace] = Field(default_factory=list)
    intake: ProductIntakeResponse | None = None


class ChatwootWebhookResponse(BaseModel):
    ok: bool
    handled: bool
    status: str = "processed"
    reason: str | None = None
    event: str | None = None
    message_id: int | str | None = None
    conversation_id: int | str | None = None
    job_id: int | None = None
    reply_sent: bool = False
    reply_preview: str | None = None
    tool_calls: list[AgentToolTrace] = Field(default_factory=list)
