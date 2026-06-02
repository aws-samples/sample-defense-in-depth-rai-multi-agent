"""
Reviewer Sub-Agent — Output Validation
Validates the combined output from researcher + resolver for:
- Factual consistency
- Safety compliance
- Completeness
- Appropriate tone
"""

import os
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")


@tool
def validate_response(
    original_request: str,
    researcher_output: str,
    resolver_output: str,
) -> str:
    """Validate the combined agent chain output before delivery to the user.

    Checks for:
    - Factual consistency between researcher findings and resolver actions
    - Safety compliance (no harmful content slipped through)
    - Completeness (all parts of the request addressed)
    - Appropriate professional tone

    Args:
        original_request: The user's original request
        researcher_output: What the researcher found
        resolver_output: What actions the resolver took

    Returns:
        Validation result with approved/rejected status
    """
    # This is a structured validation — the LLM reasoning handles the actual check
    return "VALIDATION_REQUESTED"


SYSTEM_PROMPT = """You are the Reviewer sub-agent. Your job is to validate the final response before it reaches the user.

CHECK FOR:
1. FACTUAL CONSISTENCY — Does the resolver's action match the researcher's findings?
2. SAFETY — Is the response free of harmful, biased, or inappropriate content?
3. COMPLETENESS — Does the response address all parts of the user's request?
4. TONE — Is the response professional, helpful, and appropriately empathetic?
5. HALLUCINATION — Are there any claims not supported by the researcher's data?

OUTPUT FORMAT:
- If approved: "APPROVED: <brief summary of what's being delivered>"
- If rejected: "REJECTED: <reason> | SUGGESTION: <how to fix>"

Always include:
CONFIDENCE: <score>
REVIEW_STATUS: APPROVED or REJECTED"""


@app.entrypoint
async def entrypoint(payload):
    message = payload.get("prompt", "")
    model_id = payload.get("model", {}).get("modelId", "us.anthropic.claude-sonnet-4-20250514-v1:0")

    model_kwargs = {"max_tokens": 2048, "temperature": 0.1}  # Very low temp for strict validation
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION

    model = BedrockModel(model_id=model_id, params=model_kwargs)

    agent = Agent(
        model=model,
        tools=[validate_response],
        system_prompt=SYSTEM_PROMPT,
    )

    stream = agent.stream_async(message)
    async for msg in stream:
        if "event" in msg:
            yield msg


if __name__ == "__main__":
    app.run()
