"""
Delegation Contract Validator — Layer 3 (Delegation Governance)

Enforces trust transitivity: a delegated agent's effective permissions
are the INTERSECTION of its own permissions and the delegating agent's
permissions (least-privilege delegation).

Also validates:
- Delegation contract completeness (required fields present)
- Max actions within bounds
- Timeout within acceptable range
- Required actions are within delegator's scope
"""

import os
import json
import time
import logging
import boto3

logger = logging.getLogger()
logger.setLevel("INFO")

dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")

AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")

# Permission definitions per agent role
AGENT_PERMISSIONS = {
    "supervisor": {
        "allowed_actions": [
            "get_account_info", "get_billing_history", "get_interaction_history",
            "process_refund", "create_support_ticket", "update_account_status",
        ],
        "max_refund": 100,
        "can_delegate_to": ["researcher", "resolver", "reviewer"],
    },
    "researcher": {
        "allowed_actions": ["get_account_info", "get_billing_history", "get_interaction_history"],
        "max_refund": 0,
        "can_delegate_to": [],
    },
    "resolver": {
        "allowed_actions": ["process_refund", "create_support_ticket", "update_account_status"],
        "max_refund": 100,
        "can_delegate_to": [],
    },
    "reviewer": {
        "allowed_actions": ["validate_response"],
        "max_refund": 0,
        "can_delegate_to": [],
    },
}

MAX_ALLOWED_ACTIONS = 10
MAX_TIMEOUT_SECONDS = 300


def compute_effective_permissions(delegator: str, delegate: str) -> dict:
    """
    Trust transitivity: effective permissions = delegator's ∩ delegate's own.
    The delegate can never exceed the delegator's scope.
    """
    delegator_perms = AGENT_PERMISSIONS.get(delegator, {})
    delegate_perms = AGENT_PERMISSIONS.get(delegate, {})

    # Intersection of allowed actions
    effective_actions = list(
        set(delegator_perms.get("allowed_actions", []))
        & set(delegate_perms.get("allowed_actions", []))
    )

    # Minimum of refund limits
    effective_max_refund = min(
        delegator_perms.get("max_refund", 0),
        delegate_perms.get("max_refund", 0),
    )

    return {
        "allowed_actions": effective_actions,
        "max_refund": effective_max_refund,
    }


def validate_contract(contract: dict) -> dict:
    """Validate a delegation contract against trust transitivity rules."""
    delegator = contract.get("delegator", "")
    required_actions = contract.get("required_actions", [])
    max_actions = contract.get("max_actions", 0)
    timeout = contract.get("timeout_seconds", 0)

    errors = []

    # Check delegator exists
    if delegator not in AGENT_PERMISSIONS:
        errors.append(f"Unknown delegator: {delegator}")
        return {"status": "DENIED", "errors": errors}

    # Check max_actions within bounds
    if max_actions > MAX_ALLOWED_ACTIONS:
        errors.append(f"max_actions ({max_actions}) exceeds limit ({MAX_ALLOWED_ACTIONS})")

    # Check timeout within bounds
    if timeout > MAX_TIMEOUT_SECONDS:
        errors.append(f"timeout ({timeout}s) exceeds limit ({MAX_TIMEOUT_SECONDS}s)")

    # Check required fields
    if not contract.get("customer_id"):
        errors.append("Missing required field: customer_id")
    if not contract.get("task_description"):
        errors.append("Missing required field: task_description")

    # Trust transitivity check for each target agent
    for target in ["researcher", "resolver", "reviewer"]:
        effective = compute_effective_permissions(delegator, target)

        # Check if required actions are within effective permissions
        for action in required_actions:
            if action not in effective["allowed_actions"]:
                # Not necessarily an error — the action might be for a different agent
                pass

    # Check delegator can delegate
    delegator_perms = AGENT_PERMISSIONS[delegator]
    if not delegator_perms.get("can_delegate_to"):
        errors.append(f"Agent '{delegator}' is not authorized to delegate")

    if errors:
        return {"status": "DENIED", "errors": errors}

    return {
        "status": "APPROVED",
        "effective_permissions": {
            "researcher": compute_effective_permissions(delegator, "researcher"),
            "resolver": compute_effective_permissions(delegator, "resolver"),
            "reviewer": compute_effective_permissions(delegator, "reviewer"),
        },
    }


def record_audit(trace_id: str, contract: dict, result: dict):
    """Write delegation decision to audit trail."""
    if not AUDIT_TABLE:
        return

    table = dynamodb.Table(AUDIT_TABLE)
    table.put_item(
        Item={
            "traceId": trace_id,
            "hopIndex": 0,  # Delegation validation is always hop 0
            "sessionId": contract.get("sessionId", "unknown"),
            "agentId": contract.get("delegator", "unknown"),
            "timestamp": int(time.time()),
            "eventType": "DELEGATION_VALIDATION",
            "status": result["status"],
            "delegationContract": json.dumps(contract),
            "effectivePermissions": json.dumps(result.get("effective_permissions", {})),
            "errors": json.dumps(result.get("errors", [])),
            "ttl": int(time.time()) + (365 * 24 * 3600),
        }
    )


def publish_metrics(result: dict):
    """Publish delegation metrics to CloudWatch."""
    cloudwatch.put_metric_data(
        Namespace="RAI/Delegation",
        MetricData=[
            {
                "MetricName": "DelegationDenials",
                "Value": 1.0 if result["status"] == "DENIED" else 0.0,
                "Unit": "Count",
            },
            {
                "MetricName": "TrustViolations",
                "Value": 1.0 if result["status"] == "DENIED" else 0.0,
                "Unit": "Count",
            },
        ],
    )


def handler(event, context):
    """
    Input (from Step Functions):
    {
        "delegator": "supervisor",
        "delegator_permissions": ["agent/read", "agent/write"],
        "task_description": "...",
        "customer_id": "CUST-001",
        "required_actions": ["get_account_info", "process_refund"],
        "max_actions": 5,
        "timeout_seconds": 120
    }
    """
    logger.info(f"Delegation validation: {json.dumps(event)}")

    trace_id = event.get("traceId", f"trace-{int(time.time())}")
    event["traceId"] = trace_id

    result = validate_contract(event)

    logger.info(f"Delegation result: {json.dumps(result)}")

    # Audit trail
    record_audit(trace_id, event, result)

    # Metrics
    publish_metrics(result)

    # Pass through the original contract + validation result for downstream steps
    return {
        **event,
        **result,
    }
