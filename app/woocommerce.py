from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class WooCommerceFetchConfig:
    base_url: str
    per_page: int = 100
    max_pages: int = 50
    stock_status: str = "instock,outofstock"
    orderby: str = "modified"
    order: str = "desc"
    timeout_seconds: int = 30
    page_delay_seconds: float = 0.15


class WooCommerceFetchError(RuntimeError):
    pass


class WooCommerceClient:
    def __init__(self, config: WooCommerceFetchConfig):
        self.config = config

    def fetch_products(self) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []

        for page in range(1, self.config.max_pages + 1):
            batch, total_pages = self.fetch_page(page)
            if not batch:
                break

            products.extend(batch)

            if total_pages is not None and page >= total_pages:
                break

            time.sleep(self.config.page_delay_seconds)

        return dedupe_products(products)

    def fetch_page(self, page: int) -> tuple[list[dict[str, Any]], int | None]:
        query = urlencode(
            {
                "per_page": self.config.per_page,
                "page": page,
                "orderby": self.config.orderby,
                "order": self.config.order,
                "stock_status": self.config.stock_status,
            }
        )
        url = urljoin(self.config.base_url.rstrip("/") + "/", f"wp-json/wc/store/v1/products?{query}")
        request = Request(url, headers={"accept": "application/json", "user-agent": "odranid-catalog-service/0.1"})

        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body)
                total_pages = parse_int_header(response.headers.get("x-wp-totalpages"))
        except HTTPError as exc:
            raise WooCommerceFetchError(f"WooCommerce returned HTTP {exc.code} for page {page}") from exc
        except URLError as exc:
            raise WooCommerceFetchError(f"Could not connect to WooCommerce for page {page}: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise WooCommerceFetchError(f"WooCommerce returned invalid JSON for page {page}") from exc

        if not isinstance(data, list):
            raise WooCommerceFetchError(f"WooCommerce page {page} returned {type(data).__name__}, expected list")

        return [item for item in data if isinstance(item, dict)], total_pages


def parse_int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def dedupe_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for product in products:
        product_id = product.get("id")
        if not isinstance(product_id, int) or product_id in seen:
            continue
        seen.add(product_id)
        deduped.append(product)
    return deduped


def build_client_from_settings(settings: Any) -> WooCommerceClient:
    if not settings.woocommerce_base_url:
        raise WooCommerceFetchError("ODRANID_WOOCOMMERCE_BASE_URL is not configured")
    return WooCommerceClient(
        WooCommerceFetchConfig(
            base_url=settings.woocommerce_base_url,
            per_page=settings.woocommerce_per_page,
            max_pages=settings.woocommerce_max_pages,
            stock_status=settings.woocommerce_stock_status,
        )
    )
