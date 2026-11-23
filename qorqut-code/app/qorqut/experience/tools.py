"""The tool calls of one period, read from every agent's archived conversations.

``Reading`` walks every conversation that was active in a run's period, decodes its records once and hands them to
``tool_calls``. Two reports come out of it: the calls that failed, grouped by what the error says (``problems``), and the
sequences of calls that repeat (``patterns``). Nothing is judged here: the reports count and give examples.

A call belongs to the period by its own time. A conversation is read whole, because whether a failed call was retried or
recovered depends on the calls around it.
"""
from __future__ import annotations

from datetime import datetime

from qorqut.native_archive import CaptureError
from qorqut.native_chat import store

from . import tool_calls

MAX_CONVERSATIONS = 500
GROUP_LIMIT = 40
REPEAT_LIMIT = 40
PATTERN_LIMIT = 40


def _moment(text):
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None


class Reading:
    def __init__(self, runtime, uow, run):
        self.runtime, self.connection, self.run = runtime, uow.connection, run
        self.start, self.end = _moment(run['window_start']), _moment(run['window_end'])
        self.gaps, self.names = [], {}

    def name(self, agent_id):
        if agent_id not in self.names:
            found = store.agent_identity(self.connection, agent_id)
            self.names[agent_id] = (found or {}).get('display_name') or agent_id
        return self.names[agent_id]

    def inside(self, at, fresh):
        """Whether a call made at ``at`` belongs to the period. A call with no readable time belongs to it only in a
        conversation that began inside the period."""
        when = _moment(at)
        if when is None or when.tzinfo is None:
            return fresh
        return self.start <= when < self.end

    def conversations(self):
        rows = self.connection.execute(
            'SELECT agent_id, provider, conversation_id, receipt_id, created_at FROM native_captures '
            'WHERE updated_at>=? AND created_at<? AND receipt_id IS NOT NULL ORDER BY updated_at DESC LIMIT ?',
            (self.run['window_start'], self.run['window_end'], MAX_CONVERSATIONS + 1)).fetchall()
        if len(rows) > MAX_CONVERSATIONS:
            self.gaps.append({'code': 'too_many_conversations',
                              'message': f'More than {MAX_CONVERSATIONS} conversations were active; the most recent are read.'})
        return [dict(row) for row in rows[:MAX_CONVERSATIONS]]

    def calls(self, capture):
        """Every tool call of one conversation, or ``None`` when its archive cannot be read now."""
        profile = store.active_profile(self.connection, capture['agent_id'], any_status=True)
        if profile is None:
            return None
        try:
            archive = self.runtime.native_chat.indexer.archive(profile)
            receipt = archive.load_receipt(capture['receipt_id'])
            with archive.iter_records(receipt) as records:
                return tool_calls.calls((record.value for record in records if record.kind == 'json'),
                                        provider=capture['provider'])
        except (CaptureError, ValueError, OSError):
            return None

    def sessions(self):
        """One entry per readable conversation: all its calls, and which of them fall inside the period."""
        found, unread = [], {}
        for capture in self.conversations():
            calls = self.calls(capture)
            agent = self.name(capture['agent_id'])
            if calls is None:
                unread[agent] = unread.get(agent, 0) + 1
                continue
            created = _moment(capture['created_at'])
            fresh = created is not None and created >= self.start
            found.append({'key': capture['conversation_id'], 'agent': agent, 'provider': capture['provider'], 'all': calls,
                          'calls': [call for call in calls if self.inside(call['at'], fresh)], 'fresh': fresh})
        for agent, count in sorted(unread.items()):
            self.gaps.append({'code': 'conversation_unreadable', 'agent': agent, 'count': count,
                              'message': 'These conversations could not be read now and are not in this report.'})
        return found

    def head(self, sessions):
        return {'run_id': self.run['id'], 'period': {'start': self.run['window_start'], 'end': self.run['window_end']},
                'conversations_read': len(sessions), 'calls': sum(len(session['calls']) for session in sessions)}

    def problems(self, limit=GROUP_LIMIT):
        sessions = self.sessions()
        failed, repeated = [], []
        for session in sessions:
            mine = [item for item in tool_calls.problems(session['all']) if self.inside(item['at'], session['fresh'])]
            failed.append({'key': session['key'], 'agent': session['agent'], 'problems': mine})
            for item in tool_calls.repeats(session['all']):
                if self.inside(item['at'], session['fresh']):
                    repeated.append({'agent': session['agent'], 'conversation': session['key'], **item})
        groups = tool_calls.group_problems(failed)
        repeated.sort(key=lambda item: (-item['count'], item['agent'], item['at'] or ''))
        by_agent = {}
        for session in failed:
            by_agent[session['agent']] = by_agent.get(session['agent'], 0) + len(session['problems'])
        return {**self.head(sessions), 'failed_calls': sum(by_agent.values()),
                'failed_calls_by_agent': dict(sorted(by_agent.items(), key=lambda item: (-item[1], item[0]))),
                'groups_total': len(groups), 'groups': groups[:limit],
                'repeated_calls_total': len(repeated), 'repeated_calls': repeated[:REPEAT_LIMIT], 'gaps': self.gaps}

    def patterns(self, limit=PATTERN_LIMIT):
        sessions = self.sessions()
        return {**self.head(sessions), 'patterns': tool_calls.patterns(sessions, limit=limit), 'gaps': self.gaps}


__all__ = ['Reading']
