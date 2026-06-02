// Policy: Delegation Scope Constraints
// Only authenticated users with write scope can perform write operations.

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"InvokeAgent",
  resource is AgentCore::Gateway
)
when {
  principal.hasTag("scope") &&
  principal.getTag("scope") like "*agent/write*"
};
