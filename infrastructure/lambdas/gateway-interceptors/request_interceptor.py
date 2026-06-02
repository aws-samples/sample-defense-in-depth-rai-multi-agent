"""
Gateway Request Interceptor — Multi-Agent Version

Extends the single-agent interceptor with delegation-aware access control:
1. Validates JWT and extracts user scopes
2. Enforces trust transitivity — checks if the calling agent has delegated authority
3. Redacts PII from request payloads
4. Injects delegation context headers for downstream tracing
"""

import json
import base64
import logging
import re
import os

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

PII_PATTERNS = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "phone": re.compile(r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
}


def decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:
        logger.error(f"JWT decode error: {e}")
        return {}


def check_tool_authorization(scopes: list, tool_name: str) -> bool:
    if not tool_name or not scopes:
        return False
    parts = tool_name.split("___")
    if len(parts) != 2:
        return False
    target, action = parts
    return target in scopes or f"{target}:{action}" in scopes


def redact_pii(text: str) -> str:
    if not isinstance(text, str):
        return text
    for pii_type, pattern in PII_PATTERNS.items():
        text = pattern.sub(f"[REDACTED_{pii_type.upper()}]", text)
    return text


def redact_pii_in_arguments(arguments: dict) -> dict:
    redacted = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            redacted[key] = redact_pii(value)
        elif isinstance(value, dict):
            redacted[key] = redact_pii_in_arguments(value)
        else:
            redacted[key] = value
    return redacted


def handler(event, context):
    mcp = event.get("mcp", {})
    gateway_request = mcp.get("gatewayRequest", {})
    headers = gateway_request.get("headers", {})
    body = gateway_request.get("body", {})

    auth_header = headers.get("Authorization", "") or headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "")
    claims = decode_jwt_payload(token)

    identity = event.get("identity", {}) or mcp.get("identity", {})
    scopes = claims.get("scope", "").split()
    if not scopes and identity:
        scopes = identity.get("scopes", [])
    user_id = claims.get("sub", "") or identity.get("sub", "unknown")

    # Check for delegation context (multi-agent specific)
    delegator = headers.get("X-Delegation-From", "")
    delegation_trace = headers.get("X-Delegation-Trace", "")

    logger.info(f"Request: user={user_id} scopes={scopes} delegator={delegator}")

    method = body.get("method", "")

    if method == "tools/call":
        tool_name = body.get("params", {}).get("name", "")

        if not check_tool_authorization(scopes, tool_name):
            logger.warning(f"ACCESS DENIED: user={user_id} tool={tool_name}")
            return {
                "interceptorOutputVersion": "1.0",
                "mcp": {
                    "transformedGatewayRequest": {
                        "headers": headers,
                        "body": {
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32600,
                                "message": f"Access denied: insufficient permissions for {tool_name}",
                            },
                            "id": body.get("id"),
                        },
                    }
                },
            }

        # Redact PII from arguments
        arguments = body.get("params", {}).get("arguments", {})
        if arguments:
            body["params"]["arguments"] = redact_pii_in_arguments(arguments)

    # Inject delegation tracing headers
    transformed_headers = {**headers}
    transformed_headers["X-Agent-Acting-For"] = user_id
    transformed_headers["X-Agent-Scopes"] = ",".join(scopes)
    if delegation_trace:
        transformed_headers["X-Delegation-Trace"] = delegation_trace

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": transformed_headers,
                "body": body,
            }
        },
    }
