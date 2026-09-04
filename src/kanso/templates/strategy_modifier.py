"""strategy.py — {{hyp_id}} (construct: {{construct}} attached to host {{host}}). The only file the research loop may edit.

Contract: subclass KansoModifier; kanso runs the pinned host sleeve with this modifier
attached and optimises the relative objective. A modifier is an engine actor, so its config
subclasses KansoModifierConfig, not KansoConfig. `evaluate` returns the Decision for the construct:
filter → allow: bool · overlay → scale: float in [0, 1], hedges: [Hedge(instrument, qty)] · exit → exit: bool
Read data time from `ctx.ts_event`; never a wall clock.
"""

from kanso.nautilus.strategy import Decision, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    threshold: float = 0.0


class Modifier(KansoModifier):
    construct = "{{construct}}"
    config_cls = Config

    def on_start(self) -> None:
        pass

    def evaluate(self, ctx) -> Decision:
        # Baseline stub: neutral decision (allow / scale 1.0 and no hedges / no exit).
        return Decision.neutral(self.construct)
