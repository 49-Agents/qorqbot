"""Durable owner attention and in-app delivery. No agent wakes or external calls."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from qorqut_operations.contracts import DomainError, canonical_hash, new_id
from qorqut_operations.storage.repositories import decode_row
from .ports import ABSENT

EASTERN = ZoneInfo('America/New_York')


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class SignoffNotifications:
    def __init__(self, service):
        self.service = service
        self.database = service.database

    def _scopes(self, context, uow):
        context = self.service.app.current_context(context, uow)
        if context.principal_kind != 'human':
            raise DomainError('forbidden', 'Owner attention is available only to humans')
        businesses = [dict(b) for b in uow.connection.execute('''SELECT b.id,b.name,b.slug FROM businesses b
            JOIN business_memberships m ON m.business_id=b.id WHERE m.principal_id=? AND m.active=1 AND m.is_owner=1 AND b.archived_at IS NULL''',
            (context.principal_id,)) if b['id'] in context.allowed_business_ids]
        installation = uow.repository('installation').get('service')
        if installation and installation['owner_principal_id'] == context.principal_id:
            businesses.append({'id': None, 'name': 'Global', 'slug': None})
        return context, businesses

    def _rows(self, context, uow, *, include_managed=False):
        context, businesses = self._scopes(context, uow)
        rows = []
        for business in businesses:
            scoped = replace(context, business_id=business['id'], review_business_id=business['id'])
            clause, params = self.service._visibility(scoped,uow)
            if not include_managed and self.service.ports.management is not ABSENT:
                clause += ' AND NOT ('+self.service.ports.management.managed_input_sql+')'
            found = uow.connection.execute(f'''SELECT s.* FROM signoffs s WHERE {clause}
                AND s.review_business_id IS ?
                AND s.state='pending' AND s.superseded_by_id IS NULL ORDER BY s.created_at,s.id''', [*params, business['id']])
            for raw in found:
                row = self.service._dto(decode_row(raw), uow, detail=False)
                row.update(business_slug=business['slug'], business_name=business['name'])
                rows.append(row)
        return context, businesses, rows

    def overview(self, context, *, all_items=False):
        with self.database.read() as uow:
            context, businesses, rows = self._rows(context, uow, include_managed=all_items)
            counts = []
            now = stamp(instant(self.service.clock()))
            for business in businesses:
                pending = [r for r in rows if r['review_business_id'] == business['id']]
                tasks = self.service.ports.tasks.open_assigned_count(uow, business['id'], context.principal_id, now)
                urgent = sum(bool(r['attention']['urgent'] or r['attention']['blocking']) for r in pending)
                counts.append({**business, 'signoffs': len(pending), 'tasks': tasks, 'total': len(pending)+tasks, 'urgent': urgent})
            return {'businesses': counts, 'urgent': [r for r in rows if r['attention']['urgent'] or r['attention']['blocking']]}

    def _deliver(self, owner, kind, rows, key, now, uow):
        # A disconnected dashboard should see the newest reminder, not a backlog.
        if kind == 'summary':
            uow.connection.execute("UPDATE signoff_notifications SET dismissed_at=? WHERE owner_id=? AND kind='summary' AND dismissed_at IS NULL", (now, owner))
        else:
            uow.connection.execute("""UPDATE signoff_notifications SET dismissed_at=? WHERE owner_id=? AND kind='individual'
                AND dismissed_at IS NULL AND EXISTS(SELECT 1 FROM json_each(signoff_ids_json) WHERE value=?)""", (now, owner, rows[0]['id']))
        uow.connection.execute('''INSERT OR IGNORE INTO signoff_notifications(id,owner_id,delivery_key,kind,signoff_ids_json,created_at)
            VALUES(?,?,?,?,?,?)''', (new_id(),owner,key,kind,json.dumps([r['id'] for r in rows]),now))

    def tick(self):
        now = stamp(instant(self.service.clock()))
        current = instant(now)
        eastern = current.astimezone(EASTERN)
        # signoff_notifications/signoff_notification_schedule have no capture
        # trigger; an explicit operation is the only way the host change feed's
        # coarse "signoffs" kind learns a new delivery exists, since these
        # writes are not routed through Application.invoke().
        with self.database.transaction(operation="signoffs_notification_tick") as uow:
            self.service.app.ensure_writes_open(uow)
            owners = [r[0] for r in uow.connection.execute("""SELECT DISTINCT p.id FROM principals p
                WHERE p.active=1 AND p.kind='human' AND (EXISTS(SELECT 1 FROM business_memberships m
                WHERE m.principal_id=p.id AND m.active=1 AND m.is_owner=1) OR EXISTS(
                SELECT 1 FROM installation i WHERE i.owner_principal_id=p.id))""")]
            due = set()
            for owner in owners:
                context = self.service.app.policy.context_for(owner, business_id=None, review_business_id=None, uow=uow)
                _, _, rows = self._rows(context, uow)
                ordinary = []
                for row in rows:
                    a = row['attention']
                    if not a['notify'] or (a.get('snoozed_until') and instant(a['snoozed_until']) > current):
                        continue
                    if a['urgent'] or a['blocking']:
                        if a.get('next_notify_at') and instant(a['next_notify_at']) <= current:
                            key = canonical_hash({'owner': owner, 'id': row['id'], 'due': a['next_notify_at']})
                            self._deliver(owner,'individual',[row],key,now,uow)
                            due.add(row['id'])
                    else:
                        ordinary.append(row)
                if eastern.hour >= 8:
                    date = eastern.date().isoformat()
                    schedule = uow.connection.execute('SELECT last_summary_date FROM signoff_notification_schedule WHERE owner_id=?', (owner,)).fetchone()
                    if not schedule or schedule[0] < date:
                        if ordinary:
                            self._deliver(owner,'summary',ordinary,canonical_hash({'owner':owner,'date':date}),now,uow)
                        uow.connection.execute('INSERT INTO signoff_notification_schedule(owner_id,last_summary_date) VALUES(?,?) ON CONFLICT(owner_id) DO UPDATE SET last_summary_date=excluded.last_summary_date', (owner,date))
            for identity in due:
                uow.connection.execute('UPDATE signoff_attention SET next_notify_at=?,updated_at=? WHERE signoff_id=?', (stamp(current+timedelta(hours=1)),now,identity))

    def deliveries(self, context, *, all_items=False):
        with self.database.read() as uow:
            return self.deliveries_in(context, uow, all_items=all_items)

    def deliveries_in(self, context, uow, *, all_items=False):
        """The owner's current deliveries inside a caller's unit of work (also used by the Telegram Signoffs bridge)."""
        context, _, rows = self._rows(context, uow, include_managed=all_items)
        now = instant(self.service.clock())
        visible = {r['id']:r for r in rows if r['attention']['notify'] and
            (not r['attention'].get('snoozed_until') or instant(r['attention']['snoozed_until']) <= now)}
        deliveries = []
        for raw in uow.connection.execute('SELECT * FROM signoff_notifications WHERE owner_id=? AND dismissed_at IS NULL ORDER BY created_at,id', (context.principal_id,)):
            items = [visible[i] for i in json.loads(raw['signoff_ids_json']) if i in visible]
            items = [r for r in items if bool(r['attention']['urgent'] or r['attention']['blocking']) == (raw['kind']=='individual')]
            if items:
                deliveries.append({'id':raw['id'], 'kind':raw['kind'], 'created_at':raw['created_at'], 'items':items})
        return {'items':deliveries}

    def dismiss(self, context, identity):
        with self.database.transaction(operation="signoffs_notification_dismiss") as uow:
            context, _ = self._scopes(context,uow)
            self.service.app.ensure_writes_open(uow)
            row = uow.connection.execute('SELECT * FROM signoff_notifications WHERE id=? AND owner_id=?', (identity,context.principal_id)).fetchone()
            if not row:
                raise DomainError('not_found','Notification does not exist')
            uow.connection.execute('UPDATE signoff_notifications SET dismissed_at=COALESCE(dismissed_at,?) WHERE id=?', (self.service.clock(), identity))
        return {'dismissed':True}

    def snooze(self, context, identity, until):
        try:
            value = instant(until)
        except (ValueError, TypeError, AttributeError):
            raise DomainError('validation_error','Snooze requires a timestamp with timezone') from None
        if not isinstance(until,str) or not ('+' in until[10:] or until.endswith('Z')):
            raise DomainError('validation_error','Snooze requires a timestamp with timezone')
        now = instant(self.service.clock())
        if not now < value <= now + timedelta(days=30):
            raise DomainError('validation_error','Snooze must be in the next 30 days')
        with self.database.transaction(operation="signoffs_notification_snooze") as uow:
            context,_,rows = self._rows(context,uow)
            self.service.app.ensure_writes_open(uow)
            if identity not in {r['id'] for r in rows}:
                raise DomainError('not_found','Pending signoff does not exist')
            row = uow.connection.execute('SELECT signoff_id FROM signoff_attention WHERE signoff_id=?',(identity,)).fetchone()
            if not row:
                raise DomainError('invalid_transition','This signoff has no notification settings')
            uow.connection.execute('UPDATE signoff_attention SET snoozed_until=?,next_notify_at=?,updated_at=? WHERE signoff_id=?', (stamp(value),stamp(value),stamp(now),identity))
            uow.connection.execute('''UPDATE signoff_notifications SET dismissed_at=? WHERE owner_id=? AND kind='individual'
                AND dismissed_at IS NULL AND EXISTS(SELECT 1 FROM json_each(signoff_ids_json) WHERE value=?)''', (stamp(now),context.principal_id,identity))
        return {'signoff_id':identity,'snoozed_until':stamp(value)}
