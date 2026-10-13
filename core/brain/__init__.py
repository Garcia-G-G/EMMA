"""Emma's own brain: from a transcript to a tool decision, without calling an LLM.

Platform-free by rule (BRAIN-1, rule 1): nothing under ``core/brain`` may import
``actions``, ``core.platform._mac``, PyObjC, ``subprocess`` or ``tools`` — not
directly and not through anything it imports. ``tests/test_brain_imports.py``
walks the graph and fails on a violation. The brain gets tool metadata, app
lists and thresholds as plain data from the caller; that is what lets Windows
"implement the hands" instead of rewriting the head.

Shadow mode only (BRAIN-1): a ``Decision`` is logged and compared, never executed.
"""
