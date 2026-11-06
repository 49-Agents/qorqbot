"""Owner Global cost overview: observed Context filing spend and estimated background AI spend.

The Context ``filing`` source is the usage ledger's observed call charges: a known subtotal, never an estimate and never zero for a
missing charge (see ``qorqut_context.integration.usage``). A ledger that cannot be read leaves every other source intact."""

from qorqut_agentic.behavior.tool_help import text as instruction_text

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.application.plans import schema, utc_clock

from qorqut_context.integration.usage.costs import context_charges
from qorqut_context.integration.usage.coverage import incomplete

ZERO = Decimal(0)
PLACES = Decimal("0.0001")
SOURCES = (("filing", "Context filing"), ("background_ai", "Background AI"))
COMPLETION_OUTPUT = "context_filing_receipt"
# The two final completion-only definitions (accepted host c32abc6; the programs now live in qorqut_context.integration.filing.programs): the job never calls a model, its
# model calls are the usage ledger's. A job is recognized by its immutable kind and the frozen Program.definition it stored, never
# by a name prefix or by the program currently installed; a missing or different definition stays counted.
COMPLETIONS = (("context.file", 2), ("context.instruct", 1))
COMPLETION_JOB = " OR ".join(
    f"(j.kind = '{kind}' AND json_extract(j.definition_ref_json,'$.id') = '{kind}' "
    f"AND json_type(j.definition_ref_json,'$.version') = 'integer' AND json_extract(j.definition_ref_json,'$.version') = {version} "
    f"AND json_array_length(j.definition_ref_json,'$.outputs') = 1 "
    f"AND json_extract(j.definition_ref_json,'$.outputs[0]') = '{COMPLETION_OUTPUT}')" for kind, version in COMPLETIONS)
JOB_CALLS = f"""SELECT j.kind, j.created_at, j.updated_at, json_extract(c.value,'$.usd') AS usd,
 json_extract(c.value,'$.at') AS at FROM jobs j, json_each(j.result_json,'$.calls') c
 WHERE instr(j.result_json,'"calls"')>0 AND json_type(j.result_json,'$.calls')='array' AND c.type='object'
 AND NOT COALESCE({COMPLETION_JOB}, 0)"""   # COALESCE: an absent definition field is "not recognized" (counted), never a dropped row


def moment(value):
    """Aware UTC datetime for an ISO timestamp or date; naive values are UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def usd(value):
    return str(value.quantize(PLACES, rounding=ROUND_HALF_UP))


def costs_overview(connection, today):
    """Aggregate every cost source. The Context source reads the usage ledger on the same connection; if that read fails the
    filing source is reported unavailable with a bounded `context_usage_error`, and the other sources are unaffected."""
    month_start = today.replace(day=1)
    spend = defaultdict(Decimal)
    sums = {key: [ZERO, ZERO] for key, _ in SOURCES}

    def add(key, when, value):
        spend[when] += value
        sums[key][1] += value
        if when >= month_start:
            sums[key][0] += value

    charges, context_error = None, None
    try:
        charges = context_charges(connection, today)
    except Exception as error:  # A broken usage ledger must not hide the other sources, nor be priced as zero.
        context_error = {"code": "context_usage_unavailable",
                         "message": f"The Context usage ledger could not be read ({type(error).__name__})"}
    for day, value in sorted((charges.daily if charges else {}).items()):
        add("filing", day, value)

    programs = {}
    for row in connection.execute(JOB_CALLS):
        stamp = next((value for value in (row["at"], row["updated_at"], row["created_at"]) if moment(value)), None)
        at = moment(stamp)
        if at is None or at.date() > today:
            continue
        cost = amount(row["usd"]) or ZERO
        add("background_ai", at.date(), cost)
        item = programs.setdefault(row["kind"], {"calls": 0, "month": ZERO, "total": ZERO, "last": None})
        item["calls"] += 1
        item["total"] += cost
        if at.date() >= month_start:
            item["month"] += cost
        if item["last"] is None or at > item["last"][0]:
            item["last"] = (at, stamp)

    basis = {"background_ai": "estimate"}
    sources = []
    for key, label in SOURCES:
        if key != "filing":
            sources.append({"key": key, "label": label, "month_usd": usd(sums[key][0]), "total_usd": usd(sums[key][1]),
                            "basis": basis[key]})
        elif charges is None:
            sources.append({"key": key, "label": label, "month_usd": None, "total_usd": None, "basis": "unavailable",
                            "known_subtotal": True, "unknown_calls": None, "unfinished_calls": None, "coverage": None})
        else:
            # Only an observed charge makes an amount: a period without one is null, never a zero.
            observed = any(day >= month_start for day in charges.daily)
            sources.append({"key": key, "label": label, "month_usd": usd(sums[key][0]) if observed else None,
                            "total_usd": usd(sums[key][1]) if charges.daily else None,
                            "basis": "observed" if charges.daily else "none", "known_subtotal": True,
                            "unknown_calls": charges.unknown_calls, "unfinished_calls": charges.unfinished_calls,
                            "coverage": charges.coverage})
    filing = next(source for source in sources if source["key"] == "filing")
    # Incomplete when the source is unavailable, a displayed period has no recorded charge, a call is unknown or unfinished, or
    # coverage shows an event not read to EOF or read to EOF without a call (an empty scan never establishes zero cost).
    filing_incomplete = (charges is None or filing["month_usd"] is None or filing["total_usd"] is None
                         or bool(charges.unknown_calls) or bool(charges.unfinished_calls) or incomplete(charges.coverage))
    series, days = [], sorted(spend)
    if days:
        spent = ZERO
        when = days[0]
        while when <= today:
            spent += spend.get(when, ZERO)
            series.append({"date": when.isoformat(), "spend_usd": usd(spent)})
            when += timedelta(days=1)
    return {
        "currency": "USD", "as_of": today.isoformat(), "month_start": month_start.isoformat(), "sources": sources,
        # Totals add the rounded source values so the summary table sums exactly.
        # A source without an observed amount adds nothing; the total is then a known subtotal and names what it lacks.
        "total": {**{name: usd(sum((Decimal(source[name]) for source in sources if source[name] is not None), ZERO))
                     for name in ("month_usd", "total_usd")},
                  "known_subtotal": True, "incomplete_sources": ["filing"] if filing_incomplete else []},
        "background_ai": [{"program": kind, "calls": item["calls"], "month_usd": usd(item["month"]),
                           "total_usd": usd(item["total"]), "last_at": item["last"] and item["last"][1]}
                          for kind, item in sorted(programs.items(), key=lambda pair: (-pair[1]["total"], pair[0]))],
        "series": series,
        "context_usage_error": context_error,
    }


class CostOperations:
    def __init__(self, *, clock=utc_clock):
        self.clock = clock

    def register(self, registry):
        registry.register(OperationDescriptor("costs_overview", schema(), effect_kind="read", supports_signoff=False,
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", owner_only=True,
            exposure={"dashboard"},
            description=instruction_text('costs-owner-view-global-usd-spend-by-source-observed'),
            read=self.read))
        return self

    def read(self, ctx, args, opts, uow):
        owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
        if ctx.principal_kind != "human" or not owner or owner["owner_principal_id"] != ctx.principal_id:
            raise DomainError("forbidden", "Only the installation owner may read Global costs")
        now = self.clock()
        if isinstance(now, str):
            now = datetime.fromisoformat(now.replace("Z", "+00:00"))
        return costs_overview(uow.connection, now.astimezone(timezone.utc).date())
