"""Sprint 2 eval harnesses for AMOR.

Each module here registers an :class:`EvalDescriptor` into
``admin_evals_routes._EVAL_MANIFEST`` via ``register_eval``.  The HTTP
routes pick up the runner without further glue.
"""
