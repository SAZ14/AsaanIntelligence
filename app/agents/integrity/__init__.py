"""Integrity agent: detects staff-level leakage (theft voids, comps, discounts)."""
from app.agents.integrity.analyzer import analyze_integrity, IntegrityReport

__all__ = ["analyze_integrity", "IntegrityReport"]
