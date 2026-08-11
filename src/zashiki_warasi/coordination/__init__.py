"""Cross-replica coordination primitives backed by Postgres.

Moves the single-flight tick lock and OAuth flow store out of the
Python process so multi-replica deployments (Deployment.replicaCount > 1)
can share coordination state. Both primitives are session-safe and
lazy-swept (advisory locks auto-release on session end; oauth_flows
rows expire on next pop).
"""
