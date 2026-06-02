"""
Resolver Sub-Agent — Action Execution
Executes actions (refunds, tickets) based on researcher findings.
Constrained by delegation contract's max_actions and allowed_actions.
"""

import os
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")


@tool
def process_refund(customer_id: str, amount: float, reason: str) -> str:
    """Process a refund for a customer. Amounts over $100 require human approval.

    Args:
        customer_id: The customer's unique identifier
        amount: Refund amount in USD
        reason: Reason for the refund

    Returns:
        Refund confirmation or escalation notice
    """
    if amount > 100:
        return (
            f"ESCALATED: Refund of ${amount:.2f} for {customer_id} exceeds $100 auto-approval limit. "
            f"Support ticket created for manual review. Reason: {reason}"
        )
    return f"COMPLETED: Refund of ${amount:.2f} processed for {customer_id}. Reason: {reason}"


@tool
def create_support_ticket(
    customer_id: str, subject: str, description: str, priority: str = "medium"
) -> str:
    """Create a support ticket for escalation to human agents.

    Args:
        customer_id: The customer's unique identifier
        subject: Ticket subject line
        description: Detailed description of the issue
        priority: Priority level (low, medium, high)

    Returns:
        Ticket confirmation with ID
    """
    ticket_id = abs(hash(customer_id + subject)) % 10000
    return (
        f"COMPLETED: Ticket TKT-{ticket_id:04d} created. "
        f"Customer: {customer_id}, Priority: {priority}, "
        f"Subject: '{subject}'. Estimated response: 24 hours."
    )


@tool
def update_account_status(customer_id: str, new_status: str, reason: str) -> str:
    """Update a customer's account status.

    Args:
        customer_id: The customer's unique identifier
        new_status: New status (active, suspended, closed)
        reason: Reason for the status change

    Returns:
        Status update confirmation
    """
    if new_status == "closed":
        return f"ESCALATED: Account closure for {customer_id} requires human approval. Ticket created."
    return f"COMPLETED: Account {customer_id} status updated to '{new_status}'. Reason: {reason}"


SYSTEM_PROMPT = """You are the Resolver sub-agent. Your job is to execute actions based on research findings.

RULES:
- Only perform actions explicitly authorized in the delegation contract.
- Do NOT exceed the max_actions limit.
- Refunds over $100 must be escalated (create a ticket instead).
- Account closures must always be escalated.
- Report what you did clearly with COMPLETED or ESCALATED prefix.
- Include a confidence score for your action's appropriateness.

OUTPUT FORMAT:
Always end your response with:
CONFIDENCE: <score>
ACTIONS_TAKEN: <count>"""


@app.entrypoint
async def entrypoint(payload):
    message = payload.get("prompt", "")
    model_id = payload.get("model", {}).get("modelId", "us.anthropic.claude-sonnet-4-20250514-v1:0")

    model_kwargs = {"max_tokens": 2048, "temperature": 0.2}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION

    model = BedrockModel(model_id=model_id, params=model_kwargs)

    agent = Agent(
        model=model,
        tools=[process_refund, create_support_ticket, update_account_status],
        system_prompt=SYSTEM_PROMPT,
    )

    stream = agent.stream_async(message)
    async for msg in stream:
        if "event" in msg:
            yield msg


if __name__ == "__main__":
    app.run()
