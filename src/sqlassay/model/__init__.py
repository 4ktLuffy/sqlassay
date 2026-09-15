"""Local model access: generation and embeddings, both via Ollama."""

from sqlassay.model.extract import extract_sql
from sqlassay.model.ollama import DEFAULT_BASE_URL, ModelError, OllamaChat, OllamaEmbedder

__all__ = ["DEFAULT_BASE_URL", "ModelError", "OllamaChat", "OllamaEmbedder", "extract_sql"]
