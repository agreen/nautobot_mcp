"""
Common tool handlers for both server.py and server_http.py.
This module contains the implementation logic for all MCP tools to avoid code duplication.
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

import requests

from nautobot_mcp.helpers.api_hints import (
    error_hint,
    normalize_api_path,
    paging_hint,
    schema_search_response,
)
from nautobot_mcp.utils.config import config
from nautobot_mcp.utils.repo_config import RepositoryConfig, RepositoryConfigManager

if TYPE_CHECKING:  # heavy modules (torch) - never import them at server startup
    from nautobot_mcp.helpers.endpoint_searcher_chroma import EndpointSearcherChroma
    from nautobot_mcp.helpers.nb_kb_v2 import EnhancedNautobotKnowledge

logger = logging.getLogger("nautobot_mcp")


async def handle_api_request_schema(
    query: str, n_results: int, endpoint_searcher: "EndpointSearcherChroma"
) -> str:
    """
    Handle API schema search requests.

    Returns compact, ready-to-use endpoint descriptions: method, path (relative to /api),
    accepted filters, writable body fields, and how to call the endpoint.
    """
    logger.info(
        f"Searching endpoint index for query: '{query}' with n_results={n_results}"
    )
    hits = await asyncio.to_thread(endpoint_searcher.search, query, n_results=n_results)
    return json.dumps(
        schema_search_response(hits, query, endpoint_searcher.base_url), indent=2
    )


def _send_request(
    method: str,
    url: str,
    params: Dict[str, Any],
    body: Dict[str, Any],
) -> requests.Response:
    kwargs: Dict[str, Any] = {
        "headers": config.get_headers(),
        "timeout": config.API_TIMEOUT,
        "verify": config.SSL_VERIFY,
    }
    if method in ("GET", "DELETE"):
        kwargs["params"] = params
    else:
        kwargs["json"] = body
    return requests.request(method, url, **kwargs)


def _error_result(
    status: Optional[int], message: str, hint: str, detail: Any = None
) -> str:
    result: Dict[str, Any] = {"error": True, "message": message, "hint": hint}
    if status is not None:
        result["status"] = status
    if detail not in (None, "", {}):
        result["detail"] = detail
    return json.dumps(result, indent=2)


def _fit_response(data: Any, offset: int) -> str:
    """Serialize ``data`` compactly, trimming list results if it would be too large."""
    limit = config.MAX_RESPONSE_CHARS

    def dump(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"))

    note = paging_hint(data, offset) if isinstance(data, dict) else None
    text = dump(data)
    if len(text) > limit:
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            results = data["results"]
            total = len(results)
            while len(text) > limit and len(results) > 1:
                results = results[: max(1, len(results) // 2)]
                text = dump({**data, "results": results})
            data = {**data, "results": results}
            note = (
                f"Response trimmed to {len(results)} of the {total} results returned, to fit "
                f"{limit} characters. Use a smaller limit, narrower filters, or depth=0, and "
                "page with offset for the rest."
            )
        else:
            return (
                text[:limit]
                + f"\n[hint: response cut off at {limit} characters. Use narrower filters, "
                "a smaller limit, or depth=0.]"
            )
    if note and isinstance(data, dict):
        data = {**data, "_mcp_hint": note}
    return dump(data)


async def handle_dynamic_api_request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Handle dynamic API requests to Nautobot.

    Never raises for an HTTP/network failure: the failure is returned as a JSON result with
    Nautobot's own error detail and a hint, so the caller can correct the request.

    Args:
        method: HTTP method (GET, POST, PUT, PATCH, DELETE)
        path: API endpoint path relative to /api (a leading /api is tolerated)
        params: Query parameters for GET/DELETE requests
        body: JSON body for POST/PUT/PATCH requests
    """
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return _error_result(
            None,
            f"Unsupported method: {method}",
            "Use one of GET, POST, PUT, PATCH, DELETE.",
        )
    path = normalize_api_path(path)
    params = dict(params or {})
    body = body or {}

    # GET responses nest related objects up to `depth`; a default keeps results informative.
    if method == "GET" and config.DEFAULT_DEPTH >= 0 and not path.startswith("/status"):
        params.setdefault("depth", config.DEFAULT_DEPTH)

    full_url = config.get_full_url(path)
    logger.info(f"[nautobot_dynamic_api_request] {method} {full_url}")

    try:
        response = await asyncio.to_thread(
            _send_request, method, full_url, params, body
        )
    except requests.exceptions.Timeout:
        return _error_result(
            None,
            f"Nautobot did not answer within {config.API_TIMEOUT}s.",
            "Retry with a smaller limit, narrower filters or depth=0. If it keeps timing out, "
            "the server may be slow or unreachable (raise API_TIMEOUT).",
        )
    except requests.exceptions.SSLError as e:
        return _error_result(
            None,
            f"TLS error talking to Nautobot: {e}",
            "For a self-signed certificate set SSL_VERIFY=false on the MCP server.",
        )
    except requests.exceptions.RequestException as e:
        return _error_result(
            None,
            f"Could not reach Nautobot at {config.NAUTOBOT_URL}: {e}",
            "Check NAUTOBOT_URL and network access, or run `nautobot-mcp --check`.",
        )

    try:
        data: Any = response.json()
    except ValueError:
        data = None

    if not response.ok:
        detail = data if data is not None else response.text[:1500]
        return _error_result(
            response.status_code,
            f"{method} {path} failed: HTTP {response.status_code} {response.reason}",
            error_hint(response.status_code),
            detail,
        )

    if response.status_code == 204 or (data is None and not response.text):
        return json.dumps(
            {"status": response.status_code, "result": "success (no content returned)"}
        )
    if data is None:
        data = {"response_text": response.text[:5000]}
    return _fit_response(data, int(params.get("offset") or 0))


async def handle_refresh_endpoint_index(
    endpoint_searcher: "EndpointSearcherChroma", progress_cb=None
) -> str:
    """
    Handle endpoint index refresh requests.

    Args:
        endpoint_searcher: Initialized EndpointSearcherChroma instance

    Returns:
        Success message string
    """
    logger.info("Manual endpoint index refresh triggered.")
    ok = await asyncio.to_thread(
        endpoint_searcher.initialize_collection, True, progress_cb
    )
    if not ok:
        return "Endpoint index refresh failed; see server logs. Existing index left unchanged."
    return "Endpoint index refreshed successfully."


async def handle_kb_semantic_search(
    query: str, n_results: int, nautobot_kb: "EnhancedNautobotKnowledge"
) -> str:
    """
    Handle semantic search in the Nautobot knowledge base.

    Args:
        query: Search query for Nautobot documentation and code
        n_results: Number of results to return
        nautobot_kb: Initialized EnhancedNautobotKnowledge instance

    Returns:
        JSON string containing search results with LLM optimization
    """
    logger.info(
        f"LLM-optimized semantic search for query: '{query}' with n_results={n_results}"
    )

    # Use the optimized search method directly
    optimized_results = await asyncio.to_thread(
        nautobot_kb.search_optimized_for_llm,
        query=query,
        n_results=n_results,
        max_content_length=300,  # Optimal length for LLM consumption
    )

    if optimized_results:
        response_data = {
            "results": optimized_results,
            "processing_info": {
                "total_results": len(optimized_results),
                "llm_optimized": True,
                "query": query,
            },
        }
        response_text = json.dumps(response_data, indent=2)
    else:
        response_text = json.dumps([])

    return response_text


async def handle_list_repos() -> str:
    """
    Handle repository listing requests.

    Returns:
        JSON string containing list of configured repositories
    """
    config_manager = RepositoryConfigManager()
    all_repos = config_manager.load_repositories()

    if not all_repos:
        response = {"message": "No repositories configured."}
    else:
        repos_data = []
        for repo in all_repos:
            repos_data.append(
                {
                    "name": repo.name,
                    "description": repo.description,
                    "priority": repo.priority,
                    "enabled": repo.enabled,
                    "branch": repo.branch,
                    "file_patterns": repo.file_patterns,
                }
            )

        response = {
            "repositories": repos_data,
            "total_count": len(repos_data),
        }

    return json.dumps(response, indent=2)


async def handle_add_repo(repo: str, description: Optional[str] = None) -> str:
    """
    Handle repository addition requests.

    Args:
        repo: Repository in format 'owner/name'
        description: Optional repository description

    Returns:
        JSON string containing success/error status
    """
    config_manager = RepositoryConfigManager()

    try:
        repo_config = RepositoryConfig(
            name=repo,
            description=description or f"User-added repository: {repo}",
            priority=5,
            enabled=True,
            branch="main",
            file_patterns=[".py", ".md", ".txt", ".rst", ".json"],
        )

        success = config_manager.add_user_repository(repo_config)
        if success:
            response = {
                "status": "success",
                "message": f"Added repository: {repo}",
            }
        else:
            response = {
                "status": "error",
                "message": f"Failed to add repository {repo} (may already exist)",
            }
    except Exception as e:
        response = {
            "status": "error",
            "message": f"Failed to add repository {repo}: {str(e)}",
        }

    return json.dumps(response, indent=2)


async def handle_remove_repo(repo: str) -> str:
    """
    Handle repository removal requests.

    Args:
        repo: Repository in format 'owner/name'

    Returns:
        JSON string containing success/error status
    """
    config_manager = RepositoryConfigManager()

    try:
        success = config_manager.remove_user_repository(repo)
        if success:
            response = {
                "status": "success",
                "message": f"Removed repository: {repo}",
            }
        else:
            response = {
                "status": "error",
                "message": f"Failed to remove repository {repo} (may not exist)",
            }
    except Exception as e:
        response = {
            "status": "error",
            "message": f"Failed to remove repository {repo}: {str(e)}",
        }

    return json.dumps(response, indent=2)


async def handle_update_repos(
    nautobot_kb: "EnhancedNautobotKnowledge",
    repo: Optional[str] = None,
    force: bool = False,
    progress_cb=None,
) -> str:
    """
    Handle repository update requests.

    Args:
        nautobot_kb: Initialized EnhancedNautobotKnowledge instance
        repo: Optional specific repository to update (owner/name)
        force: Force update even if no changes detected

    Returns:
        JSON string containing update results
    """
    config_manager = RepositoryConfigManager()

    try:
        if repo:
            repo_config = config_manager.get_repo_config(repo)
            if not repo_config:
                response = {
                    "status": "error",
                    "message": f"Repository {repo} not found in configuration",
                }
            else:
                success = await asyncio.to_thread(
                    nautobot_kb.update_repository, repo_config, force
                )
                if success:
                    response = {
                        "status": "success",
                        "message": f"Updated {repo}",
                    }
                else:
                    response = {
                        "status": "info",
                        "message": f"{repo} was already up to date or failed to update",
                    }
        else:
            results = await asyncio.to_thread(
                nautobot_kb.initialize_all_repositories, force, progress_cb
            )
            updated_count = sum(results.values())
            response = {
                "status": "success",
                "message": f"Updated {updated_count}/{len(results)} repositories",
                "results": {
                    repo_name: "updated" if was_updated else "skipped"
                    for repo_name, was_updated in results.items()
                },
            }
    except Exception as e:
        response = {
            "status": "error",
            "message": f"Failed to update repositories: {str(e)}",
        }

    return json.dumps(response, indent=2)


async def handle_init_repos(
    nautobot_kb: "EnhancedNautobotKnowledge", force: bool = False, progress_cb=None
) -> str:
    """
    Handle repository initialization requests.

    Args:
        nautobot_kb: Initialized EnhancedNautobotKnowledge instance
        force: Force initialization of all repositories

    Returns:
        JSON string containing initialization results
    """
    try:
        results = await asyncio.to_thread(
            nautobot_kb.initialize_all_repositories, force, progress_cb
        )
        updated_count = sum(results.values())

        response = {
            "status": "success",
            "message": f"Successfully initialized {updated_count}/{len(results)} repositories",
            "results": {
                repo_name: "updated" if was_updated else "skipped (up to date)"
                for repo_name, was_updated in results.items()
            },
            "force_enabled": force,
        }
    except Exception as e:
        response = {
            "status": "error",
            "message": f"Failed to initialize repositories: {str(e)}",
        }

    return json.dumps(response, indent=2)


async def handle_repo_status(nautobot_kb: "EnhancedNautobotKnowledge") -> str:
    """
    Handle repository status requests.

    Args:
        nautobot_kb: Initialized EnhancedNautobotKnowledge instance

    Returns:
        JSON string containing repository status information
    """
    config_manager = RepositoryConfigManager()
    all_repos = config_manager.load_repositories()

    try:
        stats = await asyncio.to_thread(nautobot_kb.get_repository_stats)

        repos_status = []
        for repo in all_repos:
            repo_stats = stats.get(repo.name, {})
            doc_count = repo_stats.get("document_count", 0)
            is_enabled = repo_stats.get("enabled", False)
            status = "indexed" if doc_count > 0 else "not_indexed"

            repo_status = {
                "name": repo.name,
                "document_count": doc_count,
                "status": status,
                "enabled": is_enabled,
                "priority": repo.priority,
                "branch": repo.branch,
            }

            if "error" in repo_stats:
                repo_status["error"] = repo_stats["error"]

            repos_status.append(repo_status)

        response = {
            "repositories": repos_status,
            "total_repositories": len(repos_status),
            "indexed_repositories": sum(
                1 for r in repos_status if r["status"] == "indexed"
            ),
            "total_documents": sum(r["document_count"] for r in repos_status),
        }
    except Exception as e:
        response = {
            "status": "error",
            "message": f"Failed to get repository status: {str(e)}",
        }

    return json.dumps(response, indent=2)
