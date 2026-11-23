"""The conversations of one period as a single timeline, for the experience extraction to read in full.

Everything the owner wrote and decided in the period, in time order, with what was said back in the same places:

* every agent's chat: the owner's messages and the agents' replies to the owner;
* the private owner threads (on Tasks, Signoffs, Tables, Projects and Focus References, side chats included): both sides;
* the owner's own notes in Task and Table discussions, on Signoffs and on Focus cards;
* the owner's Signoff decisions: what was approved, rejected or answered, with the owner's words.

Each entry names where it was written and how to open that record. The code organizes and never summarizes: an owner's
text is given whole; only a long agent reply is cut, with its length and the id that reads the rest. The rows and the
texts are read exactly as ``conversation_history_search`` reads them (``qorqut.domains.owner_inputs``), through the
coordinator's access (``access.coordinator_context``).

The report is paged by size. Only its last page carries ``end_token``, which ``experience_run_finish`` asks for: an
extraction is finished only by an agent that read the report to its end.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json

from qorqut_operations import local_time
from qorqut_operations.background.schedule import zone as zone_of
from qorqut_operations.contracts import DomainError

from qorqut.domains.owner_inputs import FIRST, FULL_TEXT_LIMIT, NOTE_ROWS, ROWS, THREAD_ROWS, _Reading
from qorqut.management.identity import installation_owner

from .access import coordinator_context

PAGE_CHARACTERS = 30_000
REPLY_LIMIT = 3_000          # an agent's reply is cut here; the owner's words never are
OWNER_LIMIT = FULL_TEXT_LIMIT
MAX_ROWS = 4_000             # of one kind in one period; more is reported as a gap, never silently dropped
DECIDED = ('approved', 'applied', 'rejected', 'answered', 'failed', 'conflicted')
REPLY_ROWS = ("SELECT 'reply:' || r.id AS item, r.completed_at AS at, r.* FROM native_replies r "
              "WHERE (r.origin='user' OR r.user_work=1)")


def end_token(run) -> str:
    return hashlib.sha256(f'{run["id"]}:{run["window_start"]}:{run["window_end"]}'.encode()).hexdigest()[:20]


class Report:
    """One read of one run's period. ``rows()`` is cheap (SQL only); texts are read from the archive for the page asked for."""

    def __init__(self, runtime, uow, run, *, zone=None):
        self.runtime, self.uow, self.run = runtime, uow, run
        self.connection = uow.connection
        self.zone = zone_of(zone or local_time.TIMEZONE)
        self.ctx = coordinator_context(runtime.application, uow)
        self.owner = installation_owner(uow)
        self.reading = _Reading(runtime.owner_inputs, self.ctx, uow, self.owner, self.ctx.principal_id)
        self.window = {'since': run['window_start'], 'until': run['window_end'], 'after': (FIRST, FIRST)}
        self.capped = []

    # ----- the rows of the period -------------------------------------------------------------------------------------

    def _select(self, sql, kind, extra):
        found = [{**dict(row), **extra} for row in self.connection.execute(
            f'SELECT * FROM ({sql}) WHERE at>=:since AND at<:until ORDER BY at, item LIMIT :limit',
            {'owner': self.owner, 'since': self.window['since'], 'until': self.window['until'], 'limit': MAX_ROWS + 1})]
        if len(found) > MAX_ROWS:
            self.capped.append(kind)
        return found[:MAX_ROWS]

    def rows(self):
        decisions = self._decisions()
        # The words of a decision are shown with the decision; the note that holds them is not listed a second time.
        said = {'signoff-note:' + identity for row in decisions for identity in self._messages(row['signoff'])}
        found = (self._select(ROWS['chat'], 'chat messages', {'channel': 'chat', 'reply': False})
                 + self._select(REPLY_ROWS, 'chat replies', {'channel': 'chat', 'reply': True})
                 + self._select(THREAD_ROWS, 'thread messages', {'channel': 'thread', 'reply': False})
                 + [row for row in self._select(NOTE_ROWS, 'notes', {'channel': 'note', 'reply': False}) if row['item'] not in said]
                 + decisions)
        found.sort(key=lambda row: (row['at'], row['item']))
        return found

    @staticmethod
    def _messages(signoff):
        decision = signoff.get('decision') or {}
        return [identity for identity in (decision.get('message_id'), *(answer.get('message_id') for answer in decision.get('answers') or ()))
                if identity]

    def _decisions(self):
        """The Signoffs decided in the period that the coordinator may read, as rows of the timeline."""
        outcomes = self.runtime.signoffs.recent_outcomes(self.ctx, self.uow, since=self.window['since'],
                                                         until=self.window['until'], limit=MAX_ROWS, side_chats=False)
        if outcomes['count'] > len(outcomes['items']):
            self.capped.append('Signoff decisions')
        return [{'item': 'signoff:' + item['id'], 'at': item['updated_at'], 'channel': 'decision', 'reply': False, 'signoff': item}
                for item in outcomes['items'] if item['state'] in DECIDED]

    # ----- one entry ----------------------------------------------------------------------------------------------------

    def when(self, at):
        local = datetime.fromisoformat(at.replace('Z', '+00:00')).astimezone(self.zone)
        return local.strftime('%A %Y-%m-%d'), local.strftime('%H:%M')

    @staticmethod
    def place(source):
        """Where a thread message or note was written, and how to open it."""
        project = f' (project {source["project"]})' if source.get('project') else ''
        title = f' "{source["title"]}"' if source.get('title') else ''
        scope = f', project {source["project_id"]}' if source.get('project_id') else ''
        return f'{source["type"].replace("_", " ")}{title}{project}', f'open: {source["type"]} {source["id"]}{scope}'

    def entry(self, row):
        """``(text block, kind)`` of one row, or ``(None, reason)`` when it cannot be shown now."""
        _, clock = self.when(row['at'])
        if row['channel'] == 'decision':
            return self._decision(row['signoff'], clock), 'decision'
        if row['channel'] == 'chat':
            agent = self.reading.agent(row['agent_id'])
            name = agent.get('name') or agent['id']
            if row['reply']:
                turn = self.reading.asked(row)
                if turn is not None and turn['redacted']:
                    return None, 'private'
                text = self.reading.reply_text(row)
                if text is None:
                    return None, 'unreadable'
                return f'[{clock}] {name.upper()} replied to the owner in chat   id {row["item"]}\n{self._cut(text, row["item"])}', 'reply'
            via = '' if row['via'] in ('chat', 'typed') else f', via {row["via"].replace("_", " ")}'
            head = f'[{clock}] OWNER to {name} in chat{via}   id {row["item"]}'
            if row['redacted']:
                return head + '\n(a possibly private input: its text is withheld)', 'owner'
            text = self.reading.turn_text(row)
            if text is None:
                return None, 'unreadable'
            return head + '\n' + text[:OWNER_LIMIT], 'owner'
        source = self.reading.source(row['source_type'], row['source_id'], row['project_id'])
        if source is None:
            return None, 'unreadable'
        where, pointer = self.place(source)
        if row['channel'] == 'thread':
            owner = (row.get('author_role') or 'owner') == 'owner'
            head = f'[{clock}] {"OWNER" if owner else "ORDUBASHY"} in the thread of {where}   id {row["item"]}   {pointer}'
            body = row['body'] or ''
            return head + '\n' + (body[:OWNER_LIMIT] if owner else self._cut(body, row['item'])), 'owner' if owner else 'reply'
        return f'[{clock}] OWNER wrote a note on {where}   id {row["item"]}   {pointer}\n{(row["body"] or "")[:OWNER_LIMIT]}', 'owner'

    @staticmethod
    def _cut(text, identity):
        if len(text) <= REPLY_LIMIT:
            return text
        return (text[:REPLY_LIMIT] + f'\n[cut here: {len(text) - REPLY_LIMIT} more characters. Read all of it with '
                f'experience_message_get id {identity}]')

    def _decision(self, signoff, clock):
        decision = signoff.get('decision') or {}
        verdict = {'approve': 'APPROVED', 'reject': 'REJECTED', 'respond': 'ANSWERED'}.get(decision.get('kind'), signoff['state'].upper())
        if signoff.get('decision_state') == 'acknowledged':
            verdict = 'ACKNOWLEDGED (read)'
        by = 'OWNER' if decision.get('by') == self.owner else 'ORDUBASHY for the owner' if decision.get('by') else 'SYSTEM'
        scope = f', project {signoff["review_project_id"]}' if signoff.get('review_project_id') else ''
        lines = [f'[{clock}] {by} {verdict} the Signoff "{signoff.get("title") or ""}" proposed by '
                 f'{signoff.get("proposer_display_name") or "an agent"}   id signoff:{signoff["id"]}   '
                 f'open: signoff {signoff["id"]}{scope}']
        chosen = decision.get('selected_choices') or []
        if chosen:
            labels = {choice['id']: choice.get('label') or choice['id'] for choice in (signoff.get('input') or {}).get('choices', [])}
            lines.append('chose: ' + ', '.join(labels.get(item, item) for item in chosen))
        for answer in decision.get('answers') or []:
            lines.append('answered ' + json.dumps({key: answer[key] for key in ('question_id', 'selected_choices', 'notes')
                                                   if answer.get(key)}, ensure_ascii=False))
        words = decision.get('notes') or decision.get('feedback')
        if words:
            lines.append('the words: ' + words[:OWNER_LIMIT])
        if signoff['state'] in ('failed', 'conflicted'):
            lines.append(f'state now: {signoff["state"]}')
        return '\n'.join(lines)

    # ----- one page -----------------------------------------------------------------------------------------------------

    def page(self, offset):
        rows = self.rows()
        if offset > len(rows):
            raise DomainError('validation_error', 'The cursor is beyond the end of this report')
        blocks, used, day, position, counts, skipped = [], 0, None, offset, {}, {}
        while position < len(rows):
            row = rows[position]
            block, kind = self.entry(row)
            position += 1
            if block is None:
                skipped[kind] = skipped.get(kind, 0) + 1
                continue
            today, _ = self.when(row['at'])
            if today != day:
                day = today
                blocks.append(f'## {today} ({self.zone.key})')
            blocks.append(block)
            counts[kind] = counts.get(kind, 0) + 1
            used += len(block)
            if used >= PAGE_CHARACTERS:
                break
        last = position >= len(rows)
        gaps = [{'code': 'too_many_rows', 'message': f'The period holds more {name} than one report reads ({MAX_ROWS}); '
                 'the oldest are in the report and the rest are not.'} for name in self.capped]
        gaps += list(self.reading.gaps.values())
        if skipped.get('unreadable'):
            gaps.append({'code': 'unreadable', 'count': skipped['unreadable'],
                         'message': 'Entries of this page whose text or record could not be read now are left out.'})
        if skipped.get('private'):
            gaps.append({'code': 'private', 'count': skipped['private'],
                         'message': 'Replies that follow a possibly private input are left out.'})
        return {'run_id': self.run['id'], 'period': {'start': self.window['since'], 'end': self.window['until']},
                'time_zone': self.zone.key, 'entries_in_period': len(rows), 'from_entry': offset + 1 if rows else 0,
                'to_entry': position, 'shown': counts, 'report': '\n\n'.join(blocks) or '(nothing was written or decided in this period)',
                'gaps': gaps, **({'end_token': end_token(self.run)} if last else {'next_cursor': str(position)})}


def message(runtime, uow, identity):
    """One entry of the report with its whole text, by its id."""
    ctx = coordinator_context(runtime.application, uow)
    owner = installation_owner(uow)
    if identity.startswith('signoff:'):
        raise DomainError('validation_error', 'A Signoff decision is opened with signoffs_get and its id, without the prefix')
    if identity.startswith('reply:'):
        found = uow.connection.execute(f'SELECT * FROM ({REPLY_ROWS}) WHERE item=?', (identity,)).fetchall()
        if len(found) != 1:
            raise DomainError('not_found', 'This reply does not exist or is not one made to the owner')
        reading = _Reading(runtime.owner_inputs, ctx, uow, owner, ctx.principal_id)
        row = {**dict(found[0]), 'channel': 'chat', 'reply': True}
        turn = reading.asked(row)
        text = None if turn is not None and turn['redacted'] else reading.reply_text(row)
        if text is None:
            raise DomainError('operation_unavailable', 'The text of this reply cannot be read now')
        return {'item': {'id': identity, 'at': row['at'], 'channel': 'chat', 'by': 'agent', 'agent': reading.agent(row['agent_id']),
                         'text': text[:FULL_TEXT_LIMIT], 'chars': len(text), 'truncated': len(text) > FULL_TEXT_LIMIT}}
    return runtime.owner_inputs.get(ctx, {'id': identity}, None, uow)


__all__ = ['PAGE_CHARACTERS', 'Report', 'end_token', 'message']
