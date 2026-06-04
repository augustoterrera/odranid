from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class EmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAIEmbeddingClient:
    api_key: str
    model: str
    timeout_seconds: int = 60

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = Request(
            "https://api.openai.com/v1/embeddings",
            data=body,
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise EmbeddingError(f"OpenAI embeddings HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise EmbeddingError(f"Could not connect to OpenAI embeddings API: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingError("OpenAI embeddings API returned invalid JSON") from exc

        data = payload.get("data")
        if not isinstance(data, list):
            raise EmbeddingError("OpenAI embeddings API response did not include data[]")

        ordered = sorted(data, key=lambda item: item.get("index", 0))
        embeddings: list[list[float]] = []
        for item in ordered:
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise EmbeddingError("OpenAI embeddings API returned an invalid embedding")
            embeddings.append([float(value) for value in embedding])

        return embeddings


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
