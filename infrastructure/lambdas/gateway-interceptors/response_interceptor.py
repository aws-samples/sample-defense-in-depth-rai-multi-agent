"""
Gateway Response Interceptor — Multi-Agent Version

1. Dynamic tool filtering by user scope
2. Response PII redaction
3. Audit metadata injection with delegation chain context
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
}


def decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def filter_tools_by_scope(tools: list, scopes: list) -> list:
    filtered = []
    for tool in tools:
        name = tool.get("name", "")
        parts = name.split("___")
        if len(parts) != 2:
            continue
        target, action = parts
        if target in scopes or f"{target}:{action}" in scopes:
            filtered.append(tool)
    return filtered


def redact_pii_in_response(data):
    if isinstance(data, str):
        for pii_type, pattern in PII_PATTERNS.items():
            data = pattern.sub(f"[REDACTED_{pii_type.upper()}]", data)
        return data
    elif isinstance(data, dict):
        return {k: redact_pii_in_response(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [redact_pii_in_response(item) for item in data]
    return data


def handler(event, context):
    mcp = event.get("mcp", {})
    gateway_response = mcp.get("gatewayResponse", {})
    gateway_request = mcp.get("gatewayRequest", {})

    status_code = gateway_response.get("statusCode", 200)
    headers = gateway_response.get("headers", {})
    body = gateway_response.get("body", {})

    req_headers = gateway_request.get("headers", {})
    auth_header = req_headers.get("Authorization", "") or req_headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "")
    claims = decode_jwt_payload(token)
    scopes = claims.get("scope", "").split()
    user_id = claims.get("sub", "unknown")

    result = body.get("result", {})

    # Dynamic tool filtering
    tools = result.get("tools", [])
    if tools:
        original_count = len(tools)
        filtered_tools = filter_tools_by_scope(tools, scopes)
        logger.info(f"TOOL FILTER: user={user_id} {original_count}->{len(filtered_tools)}")
        result["tools"] = filtered_tools
        body["result"] = result

    # PII redaction
    if "content" in result:
        result["content"] = redact_pii_in_response(result["content"])
        body["result"] = result

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": status_code,
                "headers": {
                    **headers,
                    "X-RAI-Filtered": "true",
                    "X-RAI-User": user_id,
                    "X-RAI-MultiAgent": "true",
                },
                "body": body,
            }
        },
    }
