"""Background agents for the VoiceOrderAI multi-tenant platform.

- support: health monitoring -> tickets
- marketing: weekly promo draft generation (template-driven, no LLM required)
- billing: daily usage metering for future SaaS invoicing
- fraud: toll-fraud / abuse detection -> warning tickets (conservative multi-signal)
- qa: deterministic call-transcript review -> call_quality tickets (no LLM required)
- onboarding: stalled-signup detection -> info tickets with auto-resolve
- menusync: periodic vendor catalog re-sync, records item count in settings

Scheduling is owned by ops (cron), not by this package: each module exposes
a pure run_*() entry point taking a TenantStore.
"""
