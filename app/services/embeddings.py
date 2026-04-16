from typing import Any

import httpx

from app.core.config import settings


class EmbeddingService:
    def __init__(self) -> None:
        self.api_key = settings.embedding_api_key or settings.llm_api_key
        self.base_url = (settings.embedding_base_url or settings.llm_base_url).rstrip("/")
        self.model = settings.embedding_model

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def embed_text(self, text: str) -> list[float]:
        if not self.is_configured():
            raise RuntimeError("Embedding config is incomplete.")

        payload = {
            "model": self.model,
            "input": text,
        }
        if settings.embedding_dimensions:
            payload["dimensions"] = settings.embedding_dimensions
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = httpx.Timeout(15.0, connect=5.0)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{self.base_url}/embeddings",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        items = data.get("data", [])
        if not isinstance(items, list) or not items:
            raise RuntimeError("Embedding response did not contain data.")

        embedding = items[0].get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError("Embedding response did not contain a vector.")

        return [float(value) for value in embedding]
