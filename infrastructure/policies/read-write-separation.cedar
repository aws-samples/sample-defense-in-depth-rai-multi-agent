// Policy: Escalation Scope
// Users with escalate scope can invoke the gateway for escalation operations.

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"InvokeAgent",
  resource is AgentCore::Gateway
)
when {
  principal.hasTag("scope") &&
  principal.getTag("scope") like "*agent/escalate*"
};
