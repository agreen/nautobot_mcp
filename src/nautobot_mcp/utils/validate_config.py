#!/usr/bin/env python3
"""
Configuration validation script for Nautobot MCP Server.

This script helps validate that your configuration is properly set up
and can connect to your Nautobot instance.
"""

import sys
from typing import List, Tuple

import requests
import urllib3

from nautobot_mcp.utils.config import config

# Disable SSL warnings if SSL verification is disabled
if not config.SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def validate_config() -> List[Tuple[str, bool, str]]:
    """Validate configuration and return results."""
    results = []

    # Test 1: Basic config loading
    try:
        results.append(
            (
                "Configuration loading",
                True,
                f"✓ Loaded with API_PREFIX: {config.API_PREFIX}",
            )
        )
    except Exception as e:
        results.append(
            ("Configuration loading", False, f"✗ Failed to load config: {e}")
        )
        return results

    # Test 2: Required URLs are accessible
    try:
        schema_response = requests.get(
            config.GLOBAL_TOOL_PROMPT,
            headers=config.get_headers(),
            timeout=config.SCHEMA_TIMEOUT,
            verify=config.SSL_VERIFY,
        )
        if schema_response.status_code == 200:
            try:
                schema = schema_response.json()
                paths = schema.get("paths", {})
                operations = sum(
                    1
                    for methods in paths.values()
                    for m in methods.values()
                    if isinstance(m, dict)
                )
                has_defs = bool(schema.get("definitions"))
                note = f"{len(paths)} paths, {operations} operations"
                if not has_defs:
                    note += (
                        " (no `definitions`: POST/PATCH body fields will not be listed)"
                    )
                results.append(
                    ("OpenAPI Schema", True, f"✓ {note} at {config.GLOBAL_TOOL_PROMPT}")
                )
            except ValueError:
                results.append(
                    (
                        "OpenAPI Schema",
                        False,
                        f"✗ {config.GLOBAL_TOOL_PROMPT} did not return JSON "
                        "(wrong NAUTOBOT_URL, or a login page?)",
                    )
                )
        else:
            results.append(
                (
                    "OpenAPI Schema",
                    False,
                    f"✗ Schema returned {schema_response.status_code}",
                )
            )
    except Exception as e:
        results.append(("OpenAPI Schema", False, f"✗ Cannot reach schema URL: {e}"))

    # Test 3: API endpoint connectivity
    try:
        api_response = requests.get(
            config.NAUTOBOT_BASE_URL,
            headers=config.get_headers(),
            timeout=5,
            verify=config.SSL_VERIFY,
        )
        if api_response.status_code in [
            200,
            401,
        ]:  # 401 is OK, means auth required but endpoint exists
            results.append(
                (
                    "API Endpoint",
                    True,
                    f"✓ API endpoint reachable at {config.NAUTOBOT_BASE_URL}",
                )
            )
        else:
            results.append(
                ("API Endpoint", False, f"✗ API returned {api_response.status_code}")
            )
    except Exception as e:
        results.append(("API Endpoint", False, f"✗ Cannot reach API: {e}"))

    # Test 3b: instance facts (what the nautobot://api/overview resource reports)
    from nautobot_mcp.helpers.nautobot_overview import fetch_instance_info

    info = fetch_instance_info()
    if info.get("available"):
        plugins = info.get("plugins") or {}
        results.append(
            (
                "Nautobot version",
                True,
                f"✓ Nautobot {info.get('nautobot_version')} "
                f"(Python {info.get('python_version')}), "
                f"{len(plugins)} plugin(s): {', '.join(plugins) or 'none'}",
            )
        )
    else:
        results.append(
            (
                "Nautobot version",
                False,
                f"✗ /api/status/ not readable: {info.get('error')}",
            )
        )

    # Test 4: Authentication (if token provided)
    if config.NAUTOBOT_TOKEN:
        try:
            auth_response = requests.get(
                # exists in Nautobot 1.x and 2.x; a cheap authenticated read
                f"{config.NAUTOBOT_BASE_URL}/extras/statuses/?limit=1",
                headers=config.get_headers(),
                timeout=5,
                verify=config.SSL_VERIFY,
            )
            if auth_response.status_code == 200:
                results.append(
                    ("Authentication", True, "✓ Token authentication successful")
                )
            elif auth_response.status_code == 401:
                results.append(
                    ("Authentication", False, "✗ Token authentication failed (401)")
                )
            elif auth_response.status_code == 403:
                results.append(
                    (
                        "Authentication",
                        True,
                        "✓ Token accepted, but it may not be allowed to read statuses "
                        "(403); check its permissions",
                    )
                )
            else:
                results.append(
                    (
                        "Authentication",
                        False,
                        f"✗ Unexpected response: {auth_response.status_code}",
                    )
                )
        except Exception as e:
            results.append(("Authentication", False, f"✗ Auth test failed: {e}"))
    else:
        results.append(
            ("Authentication", False, "⚠ No NAUTOBOT_TOKEN provided (optional)")
        )

    return results


def main():
    """Run configuration validation."""
    print("🔧 Nautobot MCP Server Configuration Validator")
    print("=" * 50)

    results = validate_config()

    all_passed = True
    for test_name, passed, message in results:
        status = "PASS" if passed else "FAIL"
        print(f"{test_name:20} [{status:4}] {message}")
        if not passed and "optional" not in message.lower() and "⚠" not in message:
            all_passed = False

    print("=" * 50)
    if all_passed:
        print("🎉 All critical tests passed! Your configuration looks good.")
        print("\nTo start the MCP server:")
        print("  nautobot-mcp")
    else:
        print("❌ Some tests failed. Please check your configuration.")
        print("\nNext steps:")
        print("1. Copy .env.example to .env")
        print("2. Update .env with your Nautobot instance details")
        print("3. Ensure your Nautobot instance is running and accessible")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
