"""Embedding function used for all ChromaDB collections.

Uses ChromaDB's bundled ONNX build of ``all-MiniLM-L6-v2``. This is the same model the
project used through sentence-transformers, without needing torch (a ~4 GB install that
made ``uvx`` startup impractical). The ~80 MB model is downloaded on first use into
``<data dir>/models``.
"""

from pathlib import Path

from chromadb import Documents, EmbeddingFunction, Embeddings

from nautobot_mcp.utils.path import get_model_dir


class LocalEmbeddingFunction(EmbeddingFunction):
    def __init__(self) -> None:
        # Imported lazily: pulls in onnxruntime/tokenizers.
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        ONNXMiniLM_L6_V2.DOWNLOAD_PATH = Path(get_model_dir()) / ONNXMiniLM_L6_V2.MODEL_NAME
        self._onnx = ONNXMiniLM_L6_V2()
        # Download + load the model now so failures surface during startup, not mid-search.
        self._onnx(["warmup"])

    def __call__(self, input: Documents) -> Embeddings:
        return self._onnx(input)
