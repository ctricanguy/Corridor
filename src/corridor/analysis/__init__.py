"""Analysis-only helpers — NOT wired into the engine/pipeline.

Nothing in corridor.ingest / corridor.datasources imports this package, so importing
it changes NO production behavior. It exists for offline studies (e.g. comparing
forward-quarter derivation methods) driven by scripts/, where we want to evaluate a
change before deciding whether to adopt it in the engine.
"""
