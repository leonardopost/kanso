"""strategy.py — {{hyp_id}} (construct: {{construct}} attached to host {{host}}). The only file the research loop may edit.

Contract: subclass KansoModifier; kanso runs the pinned host sleeve with this modifier
attached and optimises the relative objective. `evaluate` returns the Decision for the construct:
filter → allow: bool · overlay → scale: float in [0, 1], hedges: [{instrument, qty}] · exit → exit: bool
"""

from kanso.nautilus.strategy import Decision, KansoConfig, KansoModifier


class Config(KansoConfig):
    threshold: float = 0.0


class Modifier(KansoModifier):
    construct = "{{construct}}"
    config_cls = Config

    def on_start(self) -> None:
        pass

    def evaluate(self, ctx) -> Decision:
        # Baseline stub: neutral decision (allow / scale 1.0 and no hedges / no exit).
        return Decision.neutral(self.construct)
