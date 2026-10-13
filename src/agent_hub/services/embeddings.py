import asyncio
import hashlib
import math
import re

from agent_hub.config import settings

_model = None


def _hash_embed(text: str) -> list[float]:
    """Deterministic bag-of-words hashing embedding (offline, for tests)."""
    dim = settings.embed_dim
    vec = [0.0] * dim
    for tok in re.findall(r"\w+", text.lower()):
        h = hashlib.sha256(tok.encode()).digest()
        idx = int.from_bytes(h[:4], "big") % dim
        vec[idx] += 1.0 if h[4] % 2 == 0 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


def _fastembed(texts: list[str]) -> list[list[float]]:
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(settings.embed_model)
    return [[float(x) for x in v] for v in _model.embed(texts)]


async def embed(texts: list[str]) -> list[list[float]]:
    if settings.embedder == "hash":
        return [_hash_embed(t) for t in texts]
    return await asyncio.to_thread(_fastembed, texts)
