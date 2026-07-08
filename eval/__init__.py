"""TokenOps quality evaluation pipeline.

Offline tooling — never imported by the proxy hot path. Runs the golden
dataset through routing policies, scores results with deterministic and
LLM-as-judge evaluators, and gates CI on quality/cost regressions.
"""
