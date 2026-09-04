"""Hypothesis strategies building valid instances of every workspace model."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from hypothesis import strategies as st

from kanso.schemas import (
    Applies,
    AttachedRef,
    Card,
    Certificate,
    CertificationPlan,
    ConstraintRef,
    ConstructItem,
    ConstructRef,
    Costs,
    CostsOverride,
    CriteriaItem,
    DataAvailability,
    DateWindow,
    Deployment,
    Detected,
    Envelope,
    EvaluatedGate,
    ExcludedGate,
    Expectation,
    GateResult,
    Hypothesis,
    InstrumentEntry,
    InstrumentsFile,
    Limits,
    ModelsFile,
    ModelSpec,
    ObjectiveParams,
    ObjectiveRef,
    ObjectiveResult,
    Origins,
    Pins,
    PlanInputs,
    PlannedGate,
    Portfolio,
    Resolved,
    RiskLimits,
    Routing,
    RoutingEntry,
    RunRecord,
    SleeveRef,
    Span,
    Stage,
    Stages,
    StrategyFile,
    StrategyVersion,
    VenueModel,
    VenueOverride,
    Windows,
    embargo_days,
    parse_duration,
)

SAFE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=40
)
IDENTIFIERS = st.from_regex(r"\A[a-z][a-z0-9_]{1,12}\Z")
HYP_IDS = st.from_regex(r"\A[a-z0-9_]{3,40}\Z")
CATALOGUE_IDS = st.from_regex(r"\A[a-z][a-z0-9_]{1,20}\Z")
SHAS = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
FINITE = st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6)
POSITIVE = st.floats(
    allow_nan=False, allow_infinity=False, min_value=0.01, max_value=1e6, allow_subnormal=False
)
NON_NEGATIVE = st.floats(
    allow_nan=False, allow_infinity=False, min_value=0, max_value=1e6, allow_subnormal=False
)
PARAM_VALUES = st.one_of(st.booleans(), st.integers(-1000, 1000), FINITE, SAFE_TEXT)
PARAMS = st.dictionaries(IDENTIFIERS, PARAM_VALUES, max_size=4)
FREE_FORM = st.dictionaries(IDENTIFIERS, PARAM_VALUES, max_size=4)
TIMESTAMPS = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2030, 1, 1), timezones=st.just(UTC)
)
VENUE_CODES = st.from_regex(r"\A[A-Z]{3,4}\Z")
CURRENCIES = st.sampled_from(["USD", "EUR", "GBP", "JPY", "AUD"])
VERSION_STRINGS = st.from_regex(r"\A[0-9]{1,2}\.[0-9]{1,3}\.[0-9]{1,3}\Z")


@st.composite
def durations(draw: st.DrawFn, max_units: int = 99) -> str:
    return f"{draw(st.integers(1, max_units))}{draw(st.sampled_from('smhdw'))}"


@st.composite
def costs(draw: st.DrawFn) -> Costs:
    spread = draw(st.sampled_from(["quotes", "fixed_bps"]))
    return Costs(
        commission_bps=draw(NON_NEGATIVE),
        slippage_bps=draw(NON_NEGATIVE),
        spread=spread,
        fixed_bps=draw(NON_NEGATIVE) if spread == "fixed_bps" else None,
    )


@st.composite
def venue_models(draw: st.DrawFn, currency: str | None = None) -> VenueModel:
    account = draw(st.sampled_from(["margin", "cash"]))
    origin = st.sampled_from(["default", "broker", "venue_override", "hypothesis"])
    return VenueModel(
        venue=draw(VENUE_CODES),
        broker=draw(st.none() | IDENTIFIERS),
        account=account,
        default_leverage=draw(POSITIVE) if account == "margin" else None,
        currency=currency or draw(CURRENCIES),
        costs=draw(costs()),
        origins=Origins(account=draw(origin), currency=draw(origin), costs=draw(origin)),
    )


@st.composite
def windows(draw: st.DrawFn, horizon: str) -> Windows:
    research_start = draw(st.dates(min_value=date(2000, 1, 1), max_value=date(2015, 12, 31)))
    research = DateWindow(
        start=research_start, end=research_start + timedelta(days=draw(st.integers(1, 500)))
    )
    cert_start = research.end + timedelta(days=embargo_days(horizon) + draw(st.integers(0, 30)))
    certification = DateWindow(
        start=cert_start, end=cert_start + timedelta(days=draw(st.integers(1, 200)))
    )
    return Windows(
        research=research,
        certification=certification,
        forward={"start": certification.end + timedelta(days=draw(st.integers(1, 30)))},
    )


@st.composite
def hypotheses(draw: st.DrawFn, classified: bool | None = None) -> Hypothesis:
    resolution = draw(st.one_of(durations(), st.sampled_from(["tick", "quote", "trade"])))
    required = {
        "tick": ["trade"],
        "quote": ["quote"],
        "trade": ["trade"],
    }.get(resolution, ["bar"])
    override = draw(st.none() | costs())
    if override is not None and override.spread == "quotes" and "quote" not in required:
        required = [*required, "quote"]
    horizon = draw(durations())
    if classified is None:
        classified = draw(st.booleans())
    return Hypothesis(
        id=draw(HYP_IDS),
        title=draw(SAFE_TEXT),
        thesis=draw(SAFE_TEXT),
        mechanism=draw(
            st.sampled_from(
                [
                    "mean_reversion",
                    "momentum",
                    "microstructure",
                    "stat_arb",
                    "event",
                    "carry",
                    "vol",
                    "other",
                ]
            )
        ),
        universe=draw(st.lists(VENUE_CODES, min_size=1, max_size=4, unique=True)),
        horizon=horizon,
        resolution=resolution,
        data_requirements=required,
        costs=None if override is None else CostsOverride(**override.model_dump()),
        capital=draw(st.none() | POSITIVE),
        risk_limits=RiskLimits(
            max_position_pct=draw(POSITIVE),
            max_drawdown_pct=draw(st.floats(min_value=0.1, max_value=100)),
            max_leverage=draw(POSITIVE),
        ),
        windows=draw(windows(horizon)),
        construct=draw(construct_refs()) if classified else None,
        objective=ObjectiveRef(
            id=draw(CATALOGUE_IDS),
            params=ObjectiveParams(min_delta=draw(NON_NEGATIVE), k_se=draw(POSITIVE)),
        )
        if classified
        else None,
        constraints=[
            ConstraintRef(id="strategy_integrity", params=draw(PARAMS)),
            *([ConstraintRef(id="min_trades", params=draw(PARAMS))] if draw(st.booleans()) else []),
        ]
        if classified
        else None,
    )


@st.composite
def construct_refs(draw: st.DrawFn) -> ConstructRef:
    return ConstructRef(
        id=draw(CATALOGUE_IDS),
        host=draw(st.none() | HYP_IDS),
        params=draw(st.none() | PARAMS),
        rationale=draw(st.none() | SAFE_TEXT),
    )


@st.composite
def run_records(draw: st.DrawFn) -> RunRecord:
    started = draw(TIMESTAMPS)
    best = draw(st.none() | SHAS)
    return RunRecord(
        run_id=draw(IDENTIFIERS),
        hyp_id=draw(HYP_IDS),
        tag=f"{draw(st.integers(20000101, 20991231))}-{draw(st.integers(1, 99))}",
        lane=draw(st.from_regex(r"\A[a-z0-9_]{1,16}\Z")),
        dir=draw(SAFE_TEXT),
        base_sha=draw(SHAS),
        hypothesis_sha=draw(SHAS),
        program_sha=draw(SHAS),
        snapshot_id=draw(IDENTIFIERS),
        criteria_version=draw(VERSION_STRINGS),
        host_version=draw(st.none() | st.integers(1, 20)),
        card_budget_s=draw(POSITIVE),
        baseline_wall_s=draw(NON_NEGATIVE),
        baseline_peak_mem_gb=draw(NON_NEGATIVE),
        best_sha=best,
        best_metric=draw(FINITE) if best else None,
        started_at=started,
        ended_at=draw(st.none() | st.just(started + timedelta(seconds=1))),
    )


@st.composite
def gate_results(draw: st.DrawFn) -> GateResult:
    skipped = draw(st.none() | SAFE_TEXT)
    return GateResult(
        id=draw(CATALOGUE_IDS),
        **{"pass": True if skipped else draw(st.booleans())},
        evidence=draw(FREE_FORM),
        skipped=skipped,
    )


@st.composite
def cards(draw: st.DrawFn) -> Card:
    status = draw(st.sampled_from(["keep", "discard", "crash"]))
    return Card(
        run_id=draw(IDENTIFIERS),
        lane=draw(st.from_regex(r"\A[a-z0-9_]{1,16}\Z")),
        strategy_sha=draw(SHAS),
        metric=0.0 if status == "crash" else draw(FINITE),
        metric_se=draw(NON_NEGATIVE),
        n_trials=draw(st.integers(1, 10000)),
        n_trades=draw(st.integers(0, 10000)),
        wall_s=draw(NON_NEGATIVE),
        peak_mem_gb=draw(NON_NEGATIVE),
        status=status,
        desc=draw(
            st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=120)
        ),
        aligned=draw(st.booleans()),
        gate_results=draw(st.lists(gate_results(), max_size=3)),
        crash_tail=draw(SAFE_TEXT) if status == "crash" else None,
        venue_model=draw(venue_models()),
        created_at=draw(TIMESTAMPS),
    )


@st.composite
def plans(draw: st.DrawFn) -> CertificationPlan:
    ids = draw(st.lists(CATALOGUE_IDS, min_size=3, max_size=6, unique=True))
    stages = ["cert", "paper", "live"] + [
        draw(st.sampled_from(["cert", "paper", "live"])) for _ in ids[3:]
    ]
    span_start = draw(TIMESTAMPS)
    return CertificationPlan(
        hyp_id=draw(HYP_IDS),
        plan_version=draw(st.integers(1, 20)),
        planned_at=draw(TIMESTAMPS),
        planned_by=draw(SAFE_TEXT),
        inputs=PlanInputs(
            hypothesis_sha=draw(SHAS),
            construct=draw(construct_refs()),
            data_availability=DataAvailability(
                types=draw(st.lists(CATALOGUE_IDS, max_size=3, unique=True)),
                spans={
                    draw(IDENTIFIERS): Span(start=span_start, end=span_start + timedelta(days=1))
                },
            ),
            n_trials=draw(st.integers(0, 5000)),
        ),
        gates=[
            PlannedGate(id=gid, stage=stage, params=draw(PARAMS), rationale=draw(SAFE_TEXT))
            for gid, stage in zip(ids, stages, strict=True)
        ],
        excluded=[
            ExcludedGate(id=gid, reason=draw(SAFE_TEXT))
            for gid in draw(st.lists(CATALOGUE_IDS, max_size=2, unique=True))
            if gid not in ids
        ],
    )


@st.composite
def certificates(draw: st.DrawFn) -> Certificate:
    ids = draw(st.lists(CATALOGUE_IDS, min_size=1, max_size=5, unique=True))
    gates = []
    for gid in ids:
        skipped = draw(st.none() | SAFE_TEXT)
        gates.append(
            EvaluatedGate(
                id=gid,
                stage=draw(st.sampled_from(["card", "cert", "paper", "live"])),
                params=draw(PARAMS),
                evidence=draw(FREE_FORM),
                **{"pass": True if skipped else draw(st.booleans())},
                skipped=skipped,
            )
        )
    evaluated = [g for g in gates if g.skipped is None]
    return Certificate(
        hyp_id=draw(HYP_IDS),
        strategy_sha=draw(SHAS),
        nautilus_version=draw(VERSION_STRINGS),
        venue_model=draw(venue_models()),
        snapshot_id=draw(IDENTIFIERS),
        criteria_version=draw(VERSION_STRINGS),
        plan_version=draw(st.integers(1, 20)),
        construct=draw(construct_refs()),
        objective=ObjectiveResult(
            id=draw(CATALOGUE_IDS), value=draw(FINITE), se=draw(NON_NEGATIVE)
        ),
        gates=gates,
        n_trials=draw(st.integers(1, 5000)),
        verdict="pass" if all(g.passed for g in evaluated) else "fail",
        created_at=draw(TIMESTAMPS),
    )


@st.composite
def strategy_versions(draw: st.DrawFn, version: int, state: str) -> StrategyVersion:
    sleeve = SleeveRef(hyp_id=draw(HYP_IDS), strategy_sha=draw(SHAS))
    low = draw(FINITE)
    return StrategyVersion(
        version=version,
        sleeve=sleeve,
        attached=[
            AttachedRef(
                hyp_id=hyp,
                strategy_sha=draw(SHAS),
                construct=draw(CATALOGUE_IDS),
                params=draw(st.none() | PARAMS),
            )
            for hyp in draw(st.lists(HYP_IDS, max_size=2, unique=True))
            if hyp != sleeve.hyp_id
        ],
        config=draw(FREE_FORM),
        pins=Pins(
            kanso_version=draw(VERSION_STRINGS),
            nautilus_version=draw(VERSION_STRINGS),
            criteria_version=draw(VERSION_STRINGS),
            plan_version=draw(st.integers(1, 20)),
            snapshot_id=draw(IDENTIFIERS),
            venue_model=draw(venue_models()),
        ),
        expectation=Expectation(
            objective_id=draw(CATALOGUE_IDS),
            value=draw(FINITE),
            ci90=(low, low + draw(NON_NEGATIVE)),
            mdd_p95=draw(NON_NEGATIVE),
            window=DateWindow(start=date(2024, 1, 1), end=date(2024, 6, 30)),
        ),
        state=state,
        created_at=draw(TIMESTAMPS),
    )


@st.composite
def strategy_files(draw: st.DrawFn) -> StrategyFile:
    count = draw(st.integers(1, 4))
    states = ["retired"] * count
    if draw(st.booleans()):
        states[draw(st.integers(0, count - 1))] = draw(st.sampled_from(["paper", "promotable"]))
    if draw(st.booleans()):
        free = [i for i, s in enumerate(states) if s == "retired"]
        if free:
            states[draw(st.sampled_from(free))] = "live"
    return StrategyFile(
        id=draw(HYP_IDS),
        versions=[
            draw(strategy_versions(version=i + 1, state=state)) for i, state in enumerate(states)
        ],
    )


@st.composite
def portfolios(draw: st.DrawFn) -> Portfolio:
    limits = Limits(
        max_gross_pct=draw(POSITIVE),
        max_net_pct=draw(POSITIVE),
        per_strategy_max_pct=100.0,
        daily_loss_pct=draw(st.floats(min_value=0.1, max_value=100)),
    )
    capital = draw(st.floats(min_value=1000, max_value=1e6, allow_subnormal=False))

    def stage(names: list[str], exec_id: str) -> Stage:
        share = capital / (len(names) + 1)
        return Stage(
            exec=exec_id,
            data="replay",
            speed=draw(NON_NEGATIVE),
            capital=capital,
            kill_switch=draw(st.booleans()),
            strategies=[
                Deployment(
                    id=name,
                    version=draw(st.integers(1, 5)),
                    capital=share,
                    joined_at=draw(TIMESTAMPS),
                )
                for name in names
            ],
        )

    return Portfolio(
        stages=Stages(
            paper=stage(draw(st.lists(HYP_IDS, max_size=3, unique=True)), "sandbox"),
            live=stage([], "sandbox"),
        ),
        limits=limits,
        venues=draw(
            st.none()
            | st.dictionaries(
                VENUE_CODES,
                st.builds(VenueOverride, currency=CURRENCIES),
                max_size=2,
            )
        ),
    )


@st.composite
def envelopes(draw: st.DrawFn) -> Envelope:
    return Envelope(
        detected=Detected(
            os=draw(IDENTIFIERS),
            os_version=draw(VERSION_STRINGS),
            arch=draw(IDENTIFIERS),
            chip=draw(SAFE_TEXT),
            cores_perf=draw(st.integers(0, 64)),
            cores_eff=draw(st.integers(0, 64)),
            cores_total=draw(st.integers(1, 128)),
            mem_gb=draw(POSITIVE),
            disk_free_gb=draw(NON_NEGATIVE),
            on_ac_power=draw(st.booleans()),
            python=draw(VERSION_STRINGS),
            nautilus_version=draw(VERSION_STRINGS),
            nautilus_wheel_ok=draw(st.booleans()),
        ),
        plan={
            "live_colocated": draw(st.booleans()),
            "reserved_cores": draw(st.integers(0, 8)),
            "reserved_mem_gb": draw(NON_NEGATIVE),
            "cores_per_lane": draw(st.integers(1, 8)),
            "mem_per_lane_gb": draw(POSITIVE),
            "lanes": draw(st.integers(1, 32)),
        },
        detected_at=draw(TIMESTAMPS).isoformat(),
    )


@st.composite
def model_files(draw: st.DrawFn) -> ModelsFile:
    def entry(model_id: str) -> ModelSpec:
        return ModelSpec(
            id=model_id,
            provider=draw(IDENTIFIERS),
            protocol=draw(st.sampled_from(["anthropic", "openai_compat", "mock"])),
            base_url=draw(st.none() | SAFE_TEXT),
            api_key_env=draw(st.none() | st.from_regex(r"\A[A-Z][A-Z0-9_]{2,20}\Z")),
            tier=draw(
                st.sampled_from(["cheap", "mid", "frontier"])
                | st.lists(
                    st.sampled_from(["cheap", "mid", "frontier"]),
                    min_size=1,
                    max_size=3,
                    unique=True,
                )
            ),
            local=draw(st.booleans()),
            ctx=draw(st.integers(1, 10_000_000)),
            cost_in=draw(NON_NEGATIVE),
            cost_out=draw(NON_NEGATIVE),
            tools=draw(st.booleans()),
            script=draw(st.none() | SAFE_TEXT),
        )

    def route() -> RoutingEntry:
        return RoutingEntry(
            tier=draw(st.none() | st.sampled_from(["cheap", "mid", "frontier"])),
            effort=draw(st.none() | st.sampled_from(["none", "low", "medium", "high"])),
            max_output=draw(st.none() | st.integers(1, 100000)),
        )

    return ModelsFile(
        models=[
            entry(model_id) for model_id in draw(st.lists(IDENTIFIERS, max_size=4, unique=True))
        ],
        routing=Routing(
            classify=draw(st.none() | st.just(route())),
            propose=draw(st.none() | st.just(route())),
            align_check=draw(st.none() | st.just(route())),
            certify_plan=draw(st.none() | st.just(route())),
        ),
    )


@st.composite
def instrument_files(draw: st.DrawFn) -> InstrumentsFile:
    keys = draw(st.lists(st.from_regex(r"\A[A-Z]{1,8}\Z"), max_size=4, unique=True))
    entries = {}
    for key in keys:
        manual = draw(st.booleans())
        entries[key] = InstrumentEntry(
            nautilus_id=f"{key}.XSIM",
            asset_class=draw(
                st.sampled_from(["EQUITY", "FX", "COMMODITY", "INDEX", "CRYPTOCURRENCY"])
            ),
            resolved=None
            if manual
            else draw(
                st.none()
                | st.builds(
                    Resolved,
                    adapter=IDENTIFIERS,
                    as_of=st.dates(min_value=date(2000, 1, 1), max_value=date(2030, 1, 1)),
                    at=TIMESTAMPS,
                    checksum=SHAS,
                )
            ),
            override={"price_precision": 2} if manual else draw(FREE_FORM),
            manual=manual,
            corporate_actions=draw(st.sampled_from(["adjust_all", "none"])),
            attributes=draw(FREE_FORM),
            sources=draw(st.dictionaries(IDENTIFIERS, SAFE_TEXT, max_size=2)),
        )
    return InstrumentsFile(entries)


@st.composite
def construct_items(draw: st.DrawFn) -> ConstructItem:
    needs_host = draw(st.sampled_from(["none", "sleeve", "portfolio"]))
    return ConstructItem(
        id=draw(CATALOGUE_IDS),
        description=draw(SAFE_TEXT),
        needs_host=needs_host,
        objective_mode="absolute"
        if needs_host == "none"
        else draw(st.sampled_from(["absolute", "relative"])),
        params=draw(st.none() | FREE_FORM),
        runnable=draw(st.booleans()),
        impl=draw(st.from_regex(r"\A[a-z_][a-z0-9_]{1,8}(\.[a-z_][a-z0-9_]{1,8}){1,3}\Z")),
    )


@st.composite
def criteria_items(draw: st.DrawFn) -> CriteriaItem:
    kind = draw(st.sampled_from(["gate", "objective"]))
    names = draw(st.lists(IDENTIFIERS, max_size=3, unique=True))
    params = {name: draw(st.sampled_from(["int", "float", "duration", "bool"])) for name in names}
    ranges = {}
    for name, kind_name in params.items():
        if kind_name == "duration":
            bounds = sorted((draw(durations()), draw(durations())), key=lambda d: parse_duration(d))
            ranges[name] = (bounds[0], bounds[1])
        elif kind_name != "bool":
            low = draw(NON_NEGATIVE)
            ranges[name] = (low, low + draw(NON_NEGATIVE))
    impl = draw(st.from_regex(r"\A[a-z_][a-z0-9_]{1,8}(\.[a-z_][a-z0-9_]{1,8}){1,3}\Z"))
    if kind == "gate":
        return CriteriaItem(
            id=draw(CATALOGUE_IDS),
            kind="gate",
            stage=draw(st.sampled_from(["card", "cert", "paper", "live"])),
            required=draw(st.booleans()),
            meaningful_when=draw(SAFE_TEXT),
            params=params,
            ranges=ranges,
            impl=impl,
        )
    return CriteriaItem(
        id=draw(CATALOGUE_IDS),
        kind="objective",
        applies=Applies(
            objective_mode=draw(st.none() | st.sampled_from(["absolute", "relative"])),
            horizon=draw(st.none() | st.just({"min": "1d"})),
        ),
        priority=draw(st.integers(0, 100)),
        meaningful_when=draw(st.none() | SAFE_TEXT),
        params=params,
        ranges=ranges,
        impl=impl,
    )
