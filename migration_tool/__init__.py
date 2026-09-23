"""migration-tool - coordinator for the initial monolith -> microservices data snapshot.

Responsibilities (this phase only):
  * read business data from the legacy PostgreSQL (READ ONLY)
  * push Users, then Sales, into the new services THROUGH THEIR HTTP APIs
  * validate + reconcile (including logical Sales -> User integrity)
  * record exactly what THIS run created
  * roll back (safely) only what THIS run created

Explicit non-goals: traffic rollback, application/deploy rollback, business data
recovery after cutover, CDC, dual-write, gateways. See README.md.
"""

__version__ = "0.1.0"
