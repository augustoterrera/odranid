# Herramienta `buscar_productos`

## Objetivo

Buscar productos reales del catálogo Odranid usando el microservicio de búsqueda vectorial + facets.

El agente NO debe armar filtros técnicos ni SQL. Solo envía la consulta natural del cliente.

## Endpoint

```http
POST http://127.0.0.1:8000/search
Content-Type: application/json
```

En producción cambiar el host por la URL pública/privada del microservicio.

## Input

```json
{
  "query": "texto completo del cliente con contexto",
  "limit": 5
}
```

Schema JSON disponible en:

```txt
buscar_productos_tool.schema.json
```

### Campos

- `query`: requerido. Texto natural. Debe incluir lo que pidió el cliente y datos recordados de la conversación.
- `limit`: opcional. Usar `5` por defecto.

## Reglas Para El Agente

- Mandar solo la query natural.
- No convertir `m2` en ancho.
- No convertir números sin unidad en espesor.
- No inventar filtros.
- No agregar medidas no dichas por el cliente.
- Esperar la respuesta de la herramienta antes de recomendar.
- Presentar solo productos devueltos por la herramienta.

## Ejemplos De Llamada

### Piso Semilla

```json
{
  "query": "tenés piso semilla para cubrir 2m2",
  "limit": 5
}
```

### Piso Moneda

```json
{
  "query": "tenés piso moneda 3mm de 1.20 de ancho para gimnasio",
  "limit": 5
}
```

### Manguera

```json
{
  "query": "manguera reforzada para jardín de 20 metros",
  "limit": 5
}
```

### Ignífugo

```json
{
  "query": "piso ignífugo retardante de llama para habilitación",
  "limit": 5
}
```

## Output

El microservicio responde:

```json
{
  "query": "tenés piso semilla para cubrir 2m2",
  "hits": [
    {
      "product": {
        "id": 1011480,
        "title": "Piso De Goma Semilla Melon Espesor 3 Mm 1 M/ Ancho No Pvc",
        "link": "https://odranid.com.ar/producto/...",
        "price": 16585,
        "currency": "ARS",
        "in_stock": true,
        "rubro": "pisos",
        "category": "pisos_de_pvc",
        "floor_kind": "diseno",
        "floor_design": "semilla_melon",
        "technical_tags": ["alto_transito", "antideslizante"],
        "specs": {
          "espesor_mm": 3,
          "ancho_m": 1,
          "largo_m": 1,
          "rendimiento_m2": 1
        },
        "content": "..."
      },
      "score": 0.58,
      "matched_filters": ["rubro", "floor_kind", "floor_design"],
      "relaxed_filters": [],
      "coverage": {
        "requested_m2": 2,
        "sale_unit": "unidad",
        "coverage_m2": 1,
        "coverage_source": "rendimiento_m2",
        "rolls_needed": 2,
        "linear_meters_needed": null,
        "quantity_m2": null,
        "surplus_m2": 0,
        "needs_advisor": false,
        "message": "Para cubrir 2 m2, cada unidad cubre 1 m2. Se necesitan 2 unidades."
      }
    }
  ],
  "used_relaxation": false,
  "total_catalog_size": 533,
  "requested_m2": 2
}
```

## Cómo Interpretar La Respuesta

- Si `hits` tiene productos, el agente debe presentarlos.
- Si el cliente pidió superficie y el hit trae `coverage`, usar `coverage.message` o sus campos estructurados para recomendar cantidad.
- Si `coverage.rolls_needed` viene informado, recomendar esa cantidad de rollos/unidades.
- Si `coverage.linear_meters_needed` viene informado, recomendar metros lineales.
- Si `coverage.quantity_m2` viene informado, recomendar cantidad en m².
- Si `coverage.needs_advisor = true`, presentar el producto y derivar al asesor para confirmar cantidad.
- Si `used_relaxation = true`, explicar honestamente que no hubo coincidencia exacta y que se muestran alternativas cercanas.
- Si `relaxed_filters` contiene `ancho_m` o `espesor_mm`, no decir que el producto cumple exactamente esa medida.
- Si `hits` está vacío, usar el mensaje de no resultados del rubro correspondiente.
- No mostrar productos que no estén en `hits`.

## Comando Curl De Prueba

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{
    "query": "tenés piso moneda 3mm de 1.20 de ancho para gimnasio",
    "limit": 5
  }' | jq
```
