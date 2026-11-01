"""Apply bounded model-proposed relationships without rewriting owner decisions."""
from copy import deepcopy
from datetime import datetime, timezone


PROMPT = '''
RECONCILIATION EXTENSION (overrides the comparison restrictions above):
Compare fresh items with EACH OTHER as well as existing items. Also compare
unreviewed open existing items with each other to clear older pending findings.
The later ID may be an existing UNREVIEWED OPEN item for these resolutions.
Never change an accepted/rejected item or use a rejected answer as replacement.
Use source_at,
source/resource identifiers, quotes, dates and entity names to distinguish events.
The same day's explicit answer should replace its unanswered question in the
review queue. A later explicit correction can replace an earlier statement.
Newer alone does NOT mean truer: a partial refund does not settle the full refund,
and a recommendation is not a decision. Different entities or conditions are
not duplicates. Preserve tentative wording, exceptions and historical dates.

In addition to merges/duplicates/contradicts, return:
"resolutions": [{"earlier":"<fresh or existing id>", "later":"<fresh or unresolved existing id>",
  "kind":"answered|updated|duplicate", "reason":"Explain the evidence connecting these statements",
  "same_subject":true, "complete":true, "explicit":true, "earlier_uncertain":false,
  "replacement_state":"resolved|partial|pending|uncertain",
  "earlier_quote":"exact supporting substring of earlier owner_quote",
  "later_quote":"exact supporting substring of later owner_quote"}].
Judge each boolean independently; do not default them to true. same_subject means
the SAME person, business, contract/obligation, time period and conditions, not
just matching names. complete means no unresolved part of the older claim remains.
explicit means directly stated resolution or replacement, not your inference.
earlier_uncertain is true only for an unresolved question or stated uncertainty.
An uncertainty can be labelled fact, e.g. 'We do not know whether the invoice was
paid'. Later confirmed full payment can resolve it across days or weeks. Use
source_at, NEVER extraction/creation date, to determine which evidence is newer.
Partial payment, a promise to pay, a different invoice, and an unconfirmed agent
assertion do not resolve uncertainty about full payment of the original invoice.
Assess replacement_state independently of whether the newer text is informative:
partial means the obligation is only partly settled; pending means a promised or
anticipated resolution; uncertain means the new status is still unknown. Only
resolved means a definitive full resolution or explicit complete replacement.
For partial payments use partial even if the payment status is now better known.
Do not label such a pair updated to bypass this restriction. Keep unresolved
obligations available for owner review; being more informative is insufficient
for automatic rejection. A complete replacement of a plan can be resolved even
when execution of the new plan is future work.
answered: a later explicit fact/decision fully answers the earlier question.
updated: a later explicit statement replaces the earlier plan/fact; not merely
related commentary, an inference, partial progress, or an unanswered follow-up.
duplicate: the kept later item contains the FULL meaning of the earlier item;
The kept item may contain additional supported facts, but must retain every
claim in the earlier one. Never treat an unanswered question as a duplicate fact.
Use merges for ordinary same-tag duplicates; equivalent ideas may merge across
context/delta classification. Never turn a suggestion into a decided instruction.
Each earlier ID may appear in only one resolution. No cycles or chains of
resolutions; choose the final complete answer and point all earlier items to it.
Never discard a question unless it is actually answered, or an old fact unless
its replacement is explicit. When uncertain report a conflict or no relationship.
contradicts now accepts a fresh OR existing id in its 'existing' field. Report
fresh-versus-fresh disagreements too. Do not also merge/resolve a conflict pair.
Existing owner-reviewed items must remain unchanged; resolutions against them
are proposed updates for review. Do not rewrite candidate text in this response.
'''


def event_time(item):
    evidence = item.get('evidence') or {}
    stamp = evidence.get('source_at') or (evidence.get('source') or {}).get('at')
    try:
        value = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def supersession_supported(old, new, assessment, kind):
    """Structural evidence gates; semantic judgments still come from the model."""
    before, after = event_time(old), event_time(new)
    if not (before and after and before < after and old.get('bucket') == new.get('bucket')
            and old.get('scope') == new.get('scope') and new.get('said_by') in {'owner', 'owner_selected'}):
        return False
    if old.get('contradicts') or new.get('contradicts'):
        return False
    if old.get('scope') in {'agent', 'agent_kind'}:
        key = 'agent' if old['scope'] == 'agent' else 'agent_kind'
        identity = old.get('observed', {}).get(key)
        if not identity or identity != new.get('observed', {}).get(key):
            return False
    if any(assessment.get(field) is not True for field in ('same_subject', 'complete', 'explicit')):
        return False
    if assessment.get('replacement_state') != 'resolved':
        return False
    for item, field in ((old, 'earlier_quote'), (new, 'later_quote')):
        quote = assessment.get(field)
        evidence = item.get('evidence', {}).get('owner_quote')
        if not isinstance(quote, str) or not quote.strip() or not isinstance(evidence, str) or quote not in evidence:
            return False
    if kind == 'answered':
        return (old.get('tag') in {'question', 'fact'} and assessment.get('earlier_uncertain') is True
                and new.get('tag') in {'fact', 'decision'} and old.get('bucket') == 'context')
    return kind == 'updated' and old.get('tag') in {'fact', 'decision', 'rule'} and new.get('tag') in {'fact', 'decision', 'rule'}


def apply_resolutions(items, existing, proposals, aliases, conflicts, run_id, stats, *, supersession_mode=None):
    """Only same-day explicit resolutions auto-collapse; preserve full prior items."""
    fresh = {i['id']:i for i in items}
    known = {i['id']:deepcopy(i) for i in existing}
    def canonical(identity):
        if not isinstance(identity,str): return None
        seen=set()
        while identity in aliases and identity not in seen:
            seen.add(identity); identity=aliases[identity]
        return identity
    proposed = []
    for p in proposals if isinstance(proposals, list) else []:
        if not isinstance(p, dict):
            stats['ignored'] += 1; continue
        earlier, later = canonical(p.get('earlier')), canonical(p.get('later'))
        old, new = fresh.get(earlier, known.get(earlier)), fresh.get(later, known.get(later))
        kind, reason = p.get('kind'), p.get('reason')
        if (not old or not new or earlier == later or kind not in {'answered','updated','duplicate'}
                or not isinstance(reason,str) or not reason.strip() or len(reason)>2000
                or old.get('scope') != new.get('scope') or frozenset((earlier,later)) in conflicts):
            stats['ignored'] += 1; continue
        if kind == 'duplicate':
            valid = old.get('tag') == new.get('tag') and (old.get('bucket') == new.get('bucket') or old.get('tag')=='idea')
        else:
            before, after = event_time(old), event_time(new)
            valid = bool(before and after and before < after and new.get('said_by') in {'owner','owner_selected','human'})
            if kind == 'answered':
                valid = valid and (old.get('tag')=='question' or
                    (supersession_mode and old.get('tag')=='fact' and p.get('earlier_uncertain') is True)) and new.get('tag') in {'fact','decision'} and old.get('bucket')==new.get('bucket')=='context'
            else:
                valid = valid and old.get('tag') in {'fact','decision','rule'} and new.get('tag') in {'fact','decision','rule'} and old.get('bucket')==new.get('bucket')
        if not valid:
            stats['ignored'] += 1; continue
        proposed.append((earlier,later,kind,reason,p))
    # Ambiguous fan-out, cycles and chains do not remove anything.
    proposed=list({(p[0],p[1],p[2]):p for p in proposed}.values())
    old_ids=[p[0] for p in proposed]
    later_ids={p[1] for p in proposed}
    gone=set(); evidence=[]
    for earlier,later,kind,reason,assessment in proposed[:100]:
        if old_ids.count(earlier)!=1 or later in old_ids or earlier in later_ids:
            stats['ignored'] += 1; continue
        old,new=fresh.get(earlier,known.get(earlier)),fresh.get(later,known.get(later))
        before,after=event_time(old),event_time(new)
        same_day=bool(before and after and 0 < (after-before).total_seconds() <= 86400)
        protected=bool(old.get('reviewed') or old.get('state','open')!='open' or old.get('reconciliation_reviewed'))
        automatic=not protected and (kind=='duplicate' or same_day)
        supersession = bool(supersession_mode and earlier in known and kind in {'answered', 'updated'})
        if supersession:
            if new.get('reviewed') or new.get('state', 'open') != 'open':
                stats['ignored'] += 1
                continue
            eligible = not protected and supersession_supported(old, new, assessment, kind)
            automatic = supersession_mode == 'apply' and eligible
        # Conflicting items must remain accessible for comparison.
        automatic=automatic and not any(earlier in pair for pair in conflicts)
        relation={'kind':kind,'reason':reason,'earlier':deepcopy(old),'automatic':automatic}
        if supersession:
            relation.update(supersession=True, eligible=eligible, assessment=deepcopy(assessment))
            if any(r.get('supersession') and r.get('kind') == kind and r.get('automatic') == automatic
                   and r.get('earlier', {}).get('id') == earlier
                   and r.get('earlier', {}).get('revision') == old.get('revision')
                   for r in new.get('reconciliations', [])):
                continue
        new.setdefault('reconciliations',[]).append(relation)
        if later in known:
            evidence.append({'kind':'related','of':later,'revision':new.get('revision'),'reconciliations':[relation]})
        stats['resolved' if automatic else 'proposed_updates']=stats.get('resolved' if automatic else 'proposed_updates',0)+1
        if automatic:
            if supersession:
                stats['superseded'] = stats.get('superseded', 0) + 1
            if earlier in fresh:
                gone.add(earlier)
            else:
                evidence.append({'kind':'superseded' if supersession else 'reconciled','of':earlier,'into':later,'run':run_id,
                                 'revision':old.get('revision')})
    return [i for i in items if i['id'] not in gone],evidence
