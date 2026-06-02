"""
Supervisor Agent — Multi-Agent Orchestration
Routes customer requests to specialist sub-agents via Step Functions.
Enforces delegation contracts and trust transitivity.
"""

import os
import json
import boto3
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
ORCHESTRATOR_ARN = os.environ.get("ORCHESTRATOR_ARN", "")

sfn_client = boto3.client("stepfunctions")


@tool
def delegate_to_agents(
    task_description: str,
    customer_id: str,
    required_actions: str,
    max_actions: int = 5,
) -> str:
    """Delegate a customer support task to specialist sub-agents via the orchestration pipeline.

    This triggers the Step Functions state machine which:
    1. Validates the delegation contract (trust transitivity)
    2. Invokes the researcher agent to gather data
    3. Runs cross-agent fidelity checks
    4. Invokes the resolver agent to take action
    5. Checks for contradictions
    6. Invokes the reviewer agent for final validation

    Args:
        task_description: What needs to be accomplished
        customer_id: The customer this task is for
        required_actions: Comma-separated list of actions needed (e.g., "get_account_info,process_refund")
        max_actions: Maximum number of tool calls allowed across the chain

    Returns:
        Final reviewed response from the agent chain
    """
    if not ORCHESTRATOR_ARN:
        return "ERROR: Orchestrator not configured. Set ORCHESTRATOR_ARN environment variable."

    delegation_contract = {
        "delegator": "supervisor",
        "delegator_permissions": ["agent/read", "agent/write"],
        "task_description": task_description,
        "customer_id": customer_id,
        "required_actions": required_actions.split(","),
        "max_actions": max_actions,
        "timeout_seconds": 120,
    }

    try:
        response = sfn_client.start_sync_execution(
            stateMachineArn=ORCHESTRATOR_ARN,
            input=json.dumps(delegation_contract),
        )

        if response["status"] == "SUCCEEDED":
            output = json.loads(response.get("output", "{}"))
            return output.get("finalResponse", "Task completed successfully.")
        else:
            error = response.get("error", "Unknown error")
            cause = response.get("cause", "")
            return f"Delegation failed: {error}. {cause}"

    except Exception as e:
        return f"Orchestration error: {str(e)}"


SYSTEM_PROMPT = """You are the Supervisor AI agent for Acme Corp customer support.

AI DISCLOSURE: You are an AI assistant. Be transparent about this.

YOUR ROLE:
- You receive customer requests and delegate them to specialist sub-agents.
- You do NOT directly access customer data or perform actions yourself.
- You use the delegate_to_agents tool to route work through the governed pipeline.

DELEGATION RULES:
- Always specify the minimum required_actions needed for the task.
- Set max_actions to the smallest reasonable number.
- Never delegate tasks that require human judgment (account deletion, legal matters).
- If a request seems harmful or outside policy, refuse it directly without delegating.

ESCALATION:
- Refunds over $100 → tell the customer it requires human approval
- Account deletion → tell the customer to contact support directly
- Legal/medical/financial advice → decline and suggest professional help

Available customer IDs for demo: CUST-001, CUST-002, CUST-003"""


@app.entrypoint
async def entrypoint(payload):
    message = payload.get("prompt", "")
    model_id = payload.get("model", {}).get("modelId", "us.anthropic.claude-sonnet-4-20250514-v1:0")

    model_kwargs = {"max_tokens": 4096, "temperature": 0.3}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION

    model = BedrockModel(model_id=model_id, params=model_kwargs)

    agent = Agent(
        model=model,
        tools=[delegate_to_agents],
        system_prompt=SYSTEM_PROMPT,
    )

    stream = agent.stream_async(message)
    async for msg in stream:
        if "event" in msg:
            yield msg


if __name__ == "__main__":
    app.run()
