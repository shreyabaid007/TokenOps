"""Modal-hosted embedder for the TokenOps semantic cache.

Deployed independently:
    modal deploy modal_app/embedder.py

The proxy looks this up by name (settings.modal_embedder_app, default
'tokenops-embedder') and calls embed() over Modal RPC with a 4-second
asyncio timeout — see proxy/cache.py.

bge-small-en-v1.5 produces 384-dim normalized vectors. The model is
symmetric enough for prompt-to-prompt similarity, so no query/document
prefixes are needed for our use case.
"""

import modal

app = modal.App("tokenops-embedder")


def _preload_model() -> None:
    """Cache model weights into the image at build time.

    Running this inside the Image build (via .run_function) means the
    container starts with weights already on disk, so the first invocation
    pays only the load-into-RAM cost, not the Hugging Face download.
    """
    from sentence_transformers import SentenceTransformer

    SentenceTransformer("BAAI/bge-small-en-v1.5")


image = (
    modal.Image.debian_slim()
    .pip_install("sentence-transformers==2.7.0")
    .run_function(_preload_model)
)


# Per-container global, populated lazily on first request. Modal keeps
# containers warm between calls, so subsequent invocations skip the load.
# Module-level import of sentence_transformers is avoided — `modal deploy`
# evaluates this file locally where the package is not installed.
_model = None


@app.function(image=image, gpu="T4", min_containers=0)
def embed(texts: list[str]) -> list[list[float]]:
    """Encode a batch of strings to 384-dim normalized float vectors."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer("BAAI/bge-small-en-v1.5")

    embeddings = _model.encode(texts, normalize_embeddings=True)
    return embeddings.tolist()
