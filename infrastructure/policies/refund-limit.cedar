// Policy: Refund Limit Enforcement (Multi-Agent)
// Only users with escalate scope can trigger escalation-level operations.

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"InvokeAgent",
  resource is AgentCore::Gateway
)
when {
  principal.hasTag("scope") &&
  principal.getTag("scope") like "*agent/escalate*"
};
