"""The shipped construct catalogue: one YAML item and one implementation per construct.

Each module here exposes `CONSTRUCT`, the single instance the catalogue loader binds to
the item that names it. The four runnable constructs implement the whole interface; the
rest refuse every entry point with the seam that would make them runnable.
"""
