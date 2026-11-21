"""Automatic rules: a price condition + one order, armed by the user, sent by code when it fires.

See docs/plans/AUTO_RULES_PLAN.md and CLAUDE.md "Order safety". Nothing in this package arms a rule by
itself: arming happens only through the user's request to POST /api/rules/{id}/arm.
"""
