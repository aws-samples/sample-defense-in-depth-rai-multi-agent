// Policy: Trust Transitivity Enforcement
// A delegated agent's effective permissions are the INTERSECTION
// of its own permissions and the delegating agent's permissions.
// Supervisor can invoke the gateway; sub-agents inherit scoped access.

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"InvokeAgent",
  resource is AgentCore::Gateway
)
when {
  principal.hasTag("scope") &&
  principal.getTag("scope") like "*agent/read*"
};
