"""The agents: interpreter, explainer, verifier and personalizer, wired as an Agent Framework workflow.

Numbers come only from the stats engine. Every number a model sees has a ledger id; the explainer writes claims
that cite those ids; a deterministic checker and a semantic verifier decide which claims are shown; every step is
recorded in an audit trail. See docs/DECISIONS.md entries 12-15.

This package imports Agent Framework lazily (in ``roles``, ``workflow`` and ``pipeline``), so the CLI stays fast.
"""
