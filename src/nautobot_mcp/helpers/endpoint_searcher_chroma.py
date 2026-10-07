import hashlib
import logging
import shutil
from pathlib import Path
from typing import List, Optional

import requests
import urllib3
from chromadb import PersistentClient
from chromadb.config import Settings

from nautobot_mcp.utils import LocalEmbeddingFunction, config, get_chroma_db_path

# Disable SSL warnings if SSL verification is disabled
if not config.SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logger = logging.getLogger("endpoint_searcher_chroma")
logger.setLevel(logging.INFO)


class EndpointSearcherChroma:
    def __init__(self) -> None:
        self.base_url: str = config.GLOBAL_TOOL_PROMPT
        self.token: str = config.NAUTOBOT_TOKEN
        self.db_path: str = str(Path(get_chroma_db_path()) / "nautobot_api" / "db")
        # Why the last initialize_collection() returned False (None if it succeeded)
        self.last_error: Optional[Exception] = None

        # Local ONNX embedding model (downloaded on first use into the data dir)
        self.embedding_function = LocalEmbeddingFunction()

        self._open()

    def _open(self) -> None:
        """(Re)open the persistent ChromaDB store at ``db_path``."""
        self.client = PersistentClient(
            path=self.db_path, settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name="endpoint_collection", embedding_function=self.embedding_function
        )

    def reset(self) -> None:
        """Throw away the on-disk index and start empty.

        The index is a cache that can always be rebuilt from Nautobot's schema, so this is
        the recovery path for a corrupted store (e.g. ChromaDB "hnsw segment writer" errors).
        """
        logger.warning(f"Resetting endpoint index at {self.db_path}")
        self.client = None
        self.collection = None
        try:  # drop chroma's per-path cache of the (broken) store
            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()
        except Exception:  # noqa: BLE001 - best effort across chromadb versions
            pass
        shutil.rmtree(self.db_path, ignore_errors=True)
        Path(self.db_path).mkdir(parents=True, exist_ok=True)
        self._open()

    def _clean_metadata(self, metadata: dict) -> dict:
        """
        Ensure metadata contains only str, int, float, or bool.
        If a list contains dicts (e.g., OpenAPI parameters), serialize them.
        """
        cleaned = {}
        for key, value in metadata.items():
            if isinstance(value, (str, int, float, bool)):
                cleaned[key] = value
            elif isinstance(value, list):
                if all(isinstance(i, (str, int, float, bool)) for i in value):
                    cleaned[key] = ", ".join(map(str, value))
                elif all(isinstance(i, dict) for i in value):
                    # Serialize list of dicts as JSON string
                    import json

                    cleaned[key] = json.dumps(value)
        return cleaned

    # Chroma rejects single add/upsert calls above its max batch size (~5.4k)
    _UPSERT_BATCH_SIZE = 100

    @property
    def _hash_file(self) -> Path:
        return Path(self.db_path) / ".schema_hash"

    def initialize_collection(self, force: bool = False, progress_cb=None) -> bool:
        """Fetch the Swagger schema and (re)index it.

        Embedding thousands of endpoints is slow, so indexing is skipped when the schema
        is byte-identical to the one already indexed (unless ``force`` is set).

        Args:
            force: Re-embed even if the schema is unchanged.
            progress_cb: Optional ``callable(done, total)`` invoked as endpoints are embedded.

        Returns:
            True if the index is up to date afterwards, False if the schema could not be
            fetched or indexed (previously indexed data, if any, is left untouched).
        """
        try:
            headers = {"Authorization": f"Token {self.token}"} if self.token else {}
            response = requests.get(
                self.base_url,
                headers=headers,
                verify=config.SSL_VERIFY,
                timeout=config.SCHEMA_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            logger.info("Fetched Swagger/OpenAPI schema.")

            schema_hash = hashlib.sha256(response.content).hexdigest()
            if not force and self._schema_unchanged(schema_hash):
                logger.info(
                    "Schema unchanged since last index; skipping re-embedding "
                    f"({self.collection.count()} endpoints already indexed)."
                )
                return True

            documents = []
            metadatas = []
            ids = []
            for path, methods in data.get("paths", {}).items():
                for method, operation in methods.items():
                    combined_text = (
                        f"{method.upper()} {path} - {operation.get('description', '')}"
                    )
                    metadata = {
                        "path": path,
                        "method": method.upper(),
                        "operation_id": operation.get("operationId", ""),
                        "description": operation.get("description", ""),
                        "parameters": operation.get("parameters", []),
                    }
                    cleaned_metadata = self._clean_metadata(metadata)
                    documents.append(combined_text)
                    metadatas.append(cleaned_metadata)
                    ids.append(f"{method.upper()} {path}")

            # upsert (not add): re-running against a persisted collection must not fail
            # or duplicate entries.
            if progress_cb:
                progress_cb(0, len(ids))
            for start in range(0, len(ids), self._UPSERT_BATCH_SIZE):
                end = start + self._UPSERT_BATCH_SIZE
                self.collection.upsert(
                    documents=documents[start:end],
                    metadatas=metadatas[start:end],
                    ids=ids[start:end],
                )
                if progress_cb:
                    progress_cb(min(end, len(ids)), len(ids))
            self._write_schema_hash(schema_hash)
            logger.info(f"Successfully indexed {len(documents)} endpoints.")
            self.last_error = None
            return True
        except Exception as e:
            self.last_error = e
            logger.error(f"Failed to initialize endpoints: {e}")
            return False

    def _schema_unchanged(self, schema_hash: str) -> bool:
        try:
            return (
                self._hash_file.read_text().strip() == schema_hash
                and self.collection.count() > 0
            )
        except Exception:
            return False

    def _write_schema_hash(self, schema_hash: str) -> None:
        try:
            self._hash_file.write_text(schema_hash)
        except Exception as e:
            logger.warning(f"Could not persist schema hash: {e}")

    def search(self, query: str, n_results: int = 5) -> Optional[List[dict]]:
        try:
            result = self.collection.query(query_texts=[query], n_results=n_results)
            documents = result.get("documents") if result else None
            metadatas = result.get("metadatas") if result else None
            if documents and isinstance(documents, list) and len(documents) > 0:
                docs_list = documents[0]
                metas_list = (
                    metadatas[0]
                    if metadatas and isinstance(metadatas, list) and len(metadatas) > 0
                    else []
                )
                combined = [
                    {"document": doc, "metadata": meta}
                    for doc, meta in zip(docs_list, metas_list)
                ]
                logger.debug(
                    f"Search returned {len(combined)} results for query '{query}'."
                )
                return combined
            else:
                logger.info(f"No results found for query '{query}'.")
                return None
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return None
