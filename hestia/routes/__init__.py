"""HTTP route modules.

- ``health``      — ``/healthz`` dependency aggregation
- ``web``         — public landing, session login/logout, dashboard
- ``admin``       — admin-gated tenant management + onboarding wizard
- ``api``         — JSON API (pipeline run/status, per-tenant health)
- ``pipeline_ui`` — pipeline stepper page + HTMX status partial

Shared helpers live in :mod:`hestia.routes.deps`.
"""
