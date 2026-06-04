from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from .domain_synonyms import DEFAULT_FLOOR, FLOOR_DESIGN_RULES
from .models import ProductDocument, ProductSpecs


NOISE_PHRASES = [
    "somos odranid fabricantes e importadores hace 50 anos",
    "somos odranid fabricantes e importadores hace 60 anos",
    "priorizamos la calidad de cada producto",
    "preguntas frecuentes",
    "hacemos envios a todo el pais",
    "podes retirar tu compra sin costo extra",
    "estamos en barracas",
    "a cuadras de la autopista 9 de julio sur",
    "tu consulta no molesta",
    "su pregunta no molesta",
    "somos fabricantes e importadores directos",
    "ventas por mayor a todo el pais",
    "oferta hasta agotar stock",
]


def strip_accents(text: Any) -> str:
    normalized = unicodedata.normalize("NFD", str(text or ""))
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def norm_text(text: Any) -> str:
    return re.sub(r"\s+", " ", strip_accents(text).lower()).strip()


def slugify(text: Any) -> str:
    return re.sub(r"(^_+|_+$)", "", re.sub(r"[^a-z0-9]+", "_", norm_text(text)))


def norm_num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    raw = str(value)
    normalized = raw.replace(".", "").replace(",", ".") if "," in raw else raw
    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    if not match:
        return None
    number = float(match.group(0))
    return number if number == number else None


def to_m(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    unit_norm = norm_text(unit or "m")
    if unit_norm in {"m", "mt", "mts", "metro", "metros"}:
        return value
    if unit_norm == "cm":
        return value / 100
    if unit_norm == "mm":
        return value / 1000
    return value


def to_mm(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    unit_norm = norm_text(unit or "mm")
    if unit_norm == "mm":
        return value
    if unit_norm == "cm":
        return value * 10
    if unit_norm in {"m", "mt", "mts", "metro", "metros"}:
        return value * 1000
    return value


def parse_value_unit(text: Any) -> tuple[float | None, str | None]:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(mm|cm|m|mt|mts|m2|m²|metros?)", str(text or ""), re.I)
    if not match:
        return None, None
    return norm_num(match.group(1)), norm_text(match.group(2))


def clean_description(html_text: Any) -> str:
    text = html.unescape(str(html_text or ""))
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"[^\w\sáéíóúÁÉÍÓÚñÑüÜ/\-.,:()%°\"'$²]", " ", text)
    text = norm_text(text)
    for phrase in NOISE_PHRASES:
        text = text.replace(phrase, " ")
    return re.sub(r"\s+", " ", text).strip()


def unique_clean(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        value_str = str(value or "").strip()
        if value_str and value_str not in seen:
            seen.add(value_str)
            out.append(value_str)
    return out


def item_names(items: list[dict[str, Any]] | None) -> list[str]:
    return unique_clean(item.get("name") for item in items or [])


def item_slugs(items: list[dict[str, Any]] | None) -> list[str]:
    return unique_clean(item.get("slug") for item in items or [])


def first_image(product: dict[str, Any]) -> str | None:
    images = product.get("images") or []
    if not images:
        return None
    return images[0].get("src") or images[0].get("thumbnail")


def money_to_number(value: Any, minor_unit: int = 2) -> float | None:
    if value in (None, ""):
        return None
    raw = str(value)
    if re.fullmatch(r"\d+", raw):
        return int(raw) / (10**minor_unit)
    return norm_num(raw)


def attribute_map(product: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for attr in product.get("attributes") or []:
        key = slugify(attr.get("taxonomy") or attr.get("name")).removeprefix("pa_")
        values = unique_clean(term.get("name") for term in attr.get("terms") or [])
        if key and values:
            attrs[key] = values[0] if len(values) == 1 else values
    return attrs


def attr_value(attrs: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, list) and value:
            return value[0]
        if value:
            return value
    return None


def has_any(text: str, words: Iterable[str]) -> bool:
    return any(norm_text(word) in text for word in words)


def classify_product(product: dict[str, Any]) -> tuple[str, str, str | None]:
    category_names = item_names(product.get("categories"))
    category_slugs = item_slugs(product.get("categories"))
    primary = norm_text(" ".join([product.get("name", ""), product.get("slug", ""), *category_names, *category_slugs]))
    searchable = norm_text(" ".join([primary, clean_description(product.get("description", ""))]))

    if has_any(primary, ["piso", "pisos", "vinilico", "vinilico", "revestimiento"]):
        if has_any(primary, ["vinilico", "pisos vinilicos", "pisos-vinilicos"]):
            return "pisos", "pisos_vinilicos", None
        if has_any(primary, ["pvc", "pisos de pvc", "pisos-de-pvc"]):
            return "pisos", "pisos_de_pvc", None
        return "pisos", "pisos_de_goma", None

    if has_any(primary, ["manguera", "riego", "jardin"]):
        subcategory = "reforzadas" if has_any(searchable, ["reforzada", "reforzado"]) else None
        return "mangueras", "riego_jardin" if has_any(primary, ["riego", "jardin"]) else "mangueras", subcategory

    if has_any(primary, ["tope", "pata de goma", "patas de goma", "escalera"]):
        return "hogar", "topes_de_puertas" if has_any(primary, ["puerta", "tope"]) else "patas_de_goma", None

    if has_any(primary, ["bota", "calzado", "lluvia", "industrial"]):
        return "calzado", "para_lluvia" if "lluvia" in primary else "calzado", None

    if has_any(primary, ["mascota", "perro", "gato", "juguete"]):
        return "mascotas", "juguetes" if "juguete" in primary else "mascotas", None

    fallback = slugify(category_names[-1] if category_names else "general")
    return "general", fallback or "general", None


def is_floor(product: dict[str, Any]) -> bool:
    text = norm_text(" ".join([product.get("name", ""), product.get("slug", ""), *item_names(product.get("categories"))]))
    return "piso" in text or "vinilico" in text


def floor_taxonomy(product: dict[str, Any], attrs: dict[str, Any]) -> tuple[str | None, str | None]:
    text = norm_text(" ".join([product.get("name", ""), product.get("slug", ""), clean_description(product.get("description", ""))]))
    design_attr = norm_text(attr_value(attrs, ["nombre_del_diseno", "textura"]) or "")
    combined = f"{text} {design_attr}"
    for keywords, floor_kind, floor_design in FLOOR_DESIGN_RULES:
        if has_any(combined, keywords):
            return floor_kind, floor_design
    return DEFAULT_FLOOR


def extract_specs(product: dict[str, Any], attrs: dict[str, Any]) -> ProductSpecs:
    title = clean_description(product.get("name", ""))
    text = clean_description(" ".join([product.get("name", ""), product.get("description", "")]))
    specs = ProductSpecs()

    for key, value in attrs.items():
        value_text = " ".join(value) if isinstance(value, list) else str(value)
        parsed_value, unit = parse_value_unit(value_text)
        if parsed_value is None:
            continue
        if "espesor" in key:
            specs.espesor_mm = to_mm(parsed_value, unit)
        elif key == "ancho":
            specs.ancho_m = to_m(parsed_value, unit)
        elif key in {"largo", "longitud"}:
            specs.largo_m = to_m(parsed_value, unit)
        elif "rendimiento" in key:
            specs.rendimiento_m2 = parsed_value
        elif "diametro" in key:
            specs.diametro_mm = to_mm(parsed_value, unit)

    if specs.espesor_mm is None:
        match = re.search(r"espesor[:\s,.-]*(\d+(?:[.,]\d+)?)\s*(mm|cm|m|mt|mts)\b|(\d+(?:[.,]\d+)?)\s*mm\b", text)
        if match:
            specs.espesor_mm = to_mm(norm_num(match.group(1) or match.group(3)), match.group(2) or "mm")

    if specs.ancho_m is None:
        match = re.search(r"ancho[:\s,.-]*(\d+(?:[.,]\d+)?)\s*(m|mt|mts|cm|mm)\b|(\d+(?:[.,]\d+)?)\s*(m|mt|mts|cm|mm)\s*(?:de\s*)?ancho\b", text)
        if match:
            specs.ancho_m = to_m(norm_num(match.group(1) or match.group(3)), match.group(2) or match.group(4))

    if specs.largo_m is None:
        match = re.search(r"(?:largo|longitud)[:\s,.-]*(\d+(?:[.,]\d+)?)\s*(m|mt|mts|cm|mm)\b", text)
        if match:
            specs.largo_m = to_m(norm_num(match.group(1)), match.group(2))

    if specs.rendimiento_m2 is None:
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*(m2|m²)\b", text)
        if match:
            specs.rendimiento_m2 = norm_num(match.group(1))

    # Product titles are usually curated for the storefront and often correct
    # bad generic attributes imported from marketplaces.
    title_specs = extract_specs_from_title(title)
    specs.espesor_mm = title_specs.espesor_mm if title_specs.espesor_mm is not None else specs.espesor_mm
    specs.ancho_m = title_specs.ancho_m if title_specs.ancho_m is not None else specs.ancho_m
    specs.largo_m = title_specs.largo_m if title_specs.largo_m is not None else specs.largo_m
    specs.rendimiento_m2 = title_specs.rendimiento_m2 if title_specs.rendimiento_m2 is not None else specs.rendimiento_m2

    return specs


def extract_specs_from_title(title: str) -> ProductSpecs:
    specs = ProductSpecs()

    match = re.search(r"espesor[:\s,.-]*(\d+(?:[.,]\d+)?)\s*mm\b|(\d+(?:[.,]\d+)?)\s*mm\b", title)
    if match:
        specs.espesor_mm = norm_num(match.group(1) or match.group(2))

    # Common roll format: "1x15mt", "1.20 x 15 mts", "1,40mt x 10mt".
    match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(m|mt|mts|cm)?\s*x\s*(\d+(?:[.,]\d+)?)\s*(m|mt|mts|cm)\b",
        title,
    )
    if match:
        first_unit = match.group(2) or match.group(4)
        first = to_m(norm_num(match.group(1)), first_unit)
        second = to_m(norm_num(match.group(3)), match.group(4))
        if first is not None and second is not None and max(first, second) > 3 and min(first, second) <= 3:
            specs.ancho_m = min(first, second)
            specs.largo_m = max(first, second)
        else:
            specs.ancho_m = first
            specs.largo_m = second

    match = re.search(
        r"ancho[:\s,.-]*(\d+(?:[.,]\d+)?)\s*(m|mt|mts|cm|mm)\b|(\d+(?:[.,]\d+)?)\s*(m|mt|mts|cm|mm)\s*(?:de\s*)?ancho\b",
        title,
    )
    if match:
        specs.ancho_m = to_m(norm_num(match.group(1) or match.group(3)), match.group(2) or match.group(4))

    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(m2|m²)\b", title)
    if match:
        specs.rendimiento_m2 = norm_num(match.group(1))

    return specs


def infer_product_type(product: dict[str, Any], specs: ProductSpecs) -> str:
    text = norm_text(" ".join([product.get("name", ""), product.get("slug", ""), clean_description(product.get("description", ""))]))
    if any(needle in text for needle in ["metro lineal", "metros lineales", "mt lineal", "mts lineales", "m lineal"]) or re.search(
        r"\b(?:x|por)\s+metro\b(?!\s+cuadrado)",
        text,
    ):
        return "metro_lineal"
    if "rollo completo" in text or "rollo" in text:
        return "rollo"
    if re.search(r"\bx\s*m2\b", text) or "por m2" in text or "metro cuadrado" in text:
        return "m2"
    if "par " in text:
        return "par"
    if specs.largo_m is not None and specs.ancho_m is not None:
        return "corte"
    return "unidad"


def technical_tags(product: dict[str, Any], specs: ProductSpecs, floor_kind: str | None, floor_design: str | None) -> list[str]:
    text = norm_text(" ".join([product.get("name", ""), product.get("slug", ""), clean_description(product.get("description", ""))]))
    tags = []
    for needle, tag in [
        ("alto transito", "alto_transito"),
        ("antideslizante", "antideslizante"),
        ("reforzad", "reforzado"),
        ("industrial", "industrial"),
        ("lluvia", "lluvia"),
        ("jardin", "jardin"),
        ("pvc", "pvc"),
        ("goma", "goma"),
        ("vinilico", "vinilico"),
        ("ignifugo", "ignifugo"),
    ]:
        if needle in text:
            tags.append(tag)
    if specs.espesor_mm is not None:
        tags.append(f"espesor_{str(specs.espesor_mm).replace('.', '_')}mm")
    if floor_kind == "diseno":
        tags.append("piso_con_diseno")
    if floor_design:
        tags.append(f"diseno_{floor_design}")
    return unique_clean(tags)


def normalize_product(product: dict[str, Any]) -> ProductDocument:
    attrs = attribute_map(product)
    rubro, category, subcategory = classify_product(product)
    specs = extract_specs(product, attrs) if is_floor(product) or rubro == "mangueras" else ProductSpecs()
    if rubro == "pisos" and specs.ancho_m is not None and not 0.8 <= specs.ancho_m <= 3.1:
        specs.ancho_m = None
    floor_kind, floor_design = floor_taxonomy(product, attrs) if is_floor(product) else (None, None)
    product_type = infer_product_type(product, specs)

    categories = item_names(product.get("categories"))
    woo_tags = item_names(product.get("tags"))
    brands = item_names(product.get("brands")) or ["Odranid"]
    material = attr_value(attrs, ["material", "materiales"])
    color = attr_value(attrs, ["color", "color_principal"])
    environments = attr_value(attrs, ["ambientes", "ambiente", "usos_recomendados"])
    tech_tags = technical_tags(product, specs, floor_kind, floor_design)

    prices = product.get("prices") or {}
    price = money_to_number(prices.get("price"), prices.get("currency_minor_unit", 2))
    description = clean_description(product.get("description") or product.get("short_description") or "")

    content_lines = [
        f"TITULO: {product.get('name', '')}",
        f"RUBRO: {rubro}",
        f"CATEGORIA: {category}",
        f"TIPO PRODUCTO: {product_type}",
        f"MATERIAL: {material}" if material else None,
        f"COLOR: {color}" if color else None,
        f"AMBIENTES/USOS: {environments}" if environments else None,
        f"ESPESOR: {specs.espesor_mm} mm" if specs.espesor_mm is not None else None,
        f"ANCHO: {specs.ancho_m} m" if specs.ancho_m is not None else None,
        f"LARGO: {specs.largo_m} m" if specs.largo_m is not None else None,
        f"RENDIMIENTO: {specs.rendimiento_m2} m2" if specs.rendimiento_m2 is not None else None,
        f"PISO: {floor_kind} {floor_design or ''}".strip() if floor_kind else None,
        f"TAGS: {', '.join(tech_tags)}" if tech_tags else None,
        f"LINK: {product.get('permalink') or ''}",
        f"DESCRIPCION: {description}",
    ]
    content = "\n".join(line for line in content_lines if line)

    metadata = {
        "empresa": "odranid",
        "id_producto": product.get("id"),
        "rubro": rubro,
        "categoria_principal": category,
        "subcategoria": subcategory,
        "tipo_producto": product_type,
        "tipo_piso_categoria": floor_kind,
        "tipo_piso_diseno": floor_design,
        "espesor_mm": specs.espesor_mm,
        "ancho_m": specs.ancho_m,
        "largo_m": specs.largo_m,
        "rendimiento_m2": specs.rendimiento_m2,
        "tags": tech_tags,
        "en_stock": bool(product.get("is_in_stock")),
    }

    return ProductDocument(
        id=int(product.get("id")),
        title=product.get("name") or "",
        slug=product.get("slug"),
        link=product.get("permalink"),
        image=first_image(product),
        price=price,
        currency=prices.get("currency_code") or "ARS",
        in_stock=bool(product.get("is_in_stock")),
        stock_text=(product.get("stock_availability") or {}).get("text"),
        rubro=rubro,
        category=category,
        subcategory=subcategory,
        product_type=product_type,
        floor_kind=floor_kind,
        floor_design=floor_design,
        material=material,
        color=color,
        environments=environments,
        brands=brands,
        categories=categories,
        woo_tags=woo_tags,
        technical_tags=tech_tags,
        specs=specs,
        raw_attributes=attrs,
        content=content,
        metadata=metadata,
    )


def extract_woocommerce_products(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        products: list[dict[str, Any]] = []
        for item in payload:
            body = (item.get("json") or {}).get("body") if isinstance(item, dict) else None
            if isinstance(body, list):
                products.extend(body)
            elif isinstance(item, dict) and (item.get("id") or item.get("name")):
                products.append(item)
        return products
    if isinstance(payload, dict) and isinstance(payload.get("body"), list):
        return payload["body"]
    return []
