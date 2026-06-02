"""
Researcher Sub-Agent — Data Retrieval & Summarization
Retrieves customer data and produces summaries with confidence scores.
Outputs include a fidelity hash for chain-of-custody verification.
"""

import os
import hashlib
import json
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")


@tool
def get_account_info(customer_id: str) -> str:
    """Get account information for a customer.

    Args:
        customer_id: The customer's unique identifier

    Returns:
        Account information string
    """
    accounts = {
        "CUST-001": "Name: [REDACTED], Plan: Premium, Status: Active, Since: 2022, Tier: Gold",
        "CUST-002": "Name: [REDACTED], Plan: Standard, Status: Active, Since: 2023, Tier: Silver",
        "CUST-003": "Name: [REDACTED], Plan: Basic, Status: Suspended, Since: 2024, Tier: Bronze",
    }
    return accounts.get(customer_id, f"No account found for {customer_id}")


@tool
def get_billing_history(customer_id: str, months: int = 3) -> str:
    """Get billing history for a customer.

    Args:
        customer_id: The customer's unique identifier
        months: Number of months of history to retrieve

    Returns:
        Billing history summary
    """
    if customer_id == "CUST-001":
        return f"Last {months} months: Jan $150, Feb $150, Mar $150. All paid on time. Total: $450"
    elif customer_id == "CUST-002":
        return f"Last {months} months: Jan $75, Feb $75, Mar $75 (pending). Total: $225"
    return f"No billing records for {customer_id}"


@tool
def get_interaction_history(customer_id: str) -> str:
    """Get recent support interaction history for a customer.

    Args:
        customer_id: The customer's unique identifier

    Returns:
        Recent interaction summary
    """
    interactions = {
        "CUST-001": "3 interactions in last 30 days: billing inquiry (resolved), feature request (noted), refund request (pending)",
        "CUST-002": "1 interaction in last 30 days: password reset (resolved)",
        "CUST-003": "0 interactions in last 30 days (account suspended)",
    }
    return interactions.get(customer_id, f"No interaction history for {customer_id}")


SYSTEM_PROMPT = """You are the Researcher sub-agent. Your job is to gather and summarize customer data.

RULES:
- Only retrieve data for the customer_id provided in the delegation contract.
- Summarize findings concisely and factually.
- Do NOT make recommendations or take actions — that's the Resolver's job.
- Do NOT hallucinate data. If information is unavailable, say so.
- Include a confidence score (0.0-1.0) indicating how complete your research is.

OUTPUT FORMAT:
Always end your response with:
CONFIDENCE: <score>
FIDELITY_HASH: <sha256 of your factual claims>"""


@app.entrypoint
async def entrypoint(payload):
    message = payload.get("prompt", "")
    model_id = payload.get("model", {}).get("modelId", "us.anthropic.claude-sonnet-4-20250514-v1:0")

    model_kwargs = {"max_tokens": 2048, "temperature": 0.1}  # Low temp for factual retrieval
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION

    model = BedrockModel(model_id=model_id, params=model_kwargs)

    agent = Agent(
        model=model,
        tools=[get_account_info, get_billing_history, get_interaction_history],
        system_prompt=SYSTEM_PROMPT,
    )

    stream = agent.stream_async(message)
    async for msg in stream:
        if "event" in msg:
            yield msg


if __name__ == "__main__":
    app.run()
