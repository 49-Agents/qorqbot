"""Task and Action counts behind the host's combined progress views.

Four small readers, each the exact loop a progress read ran inline. The caller owns the open unit of work, the series
and the totals; it supplies either its ``count(key, value)`` callback or the dictionaries to update, and calls each reader
at its own point of the read. A reader runs one query with the bounds shown and counts every row once. Dates arrive
already normalized in the stored rows (``occurred_date`` is the local calendar day), so no date or time zone is parsed here.
"""


def project_task_completions(uow, business_id, count):
    """Each Task first completed in the Project: ``count("tasks_done", completed_at)`` once per row."""
    for row in uow.connection.execute("SELECT completed_at FROM task_completions WHERE business_id=?", (business_id,)):
        count("tasks_done", row[0])


def project_action_counts(uow, business_id, totals, undated, series):
    """Actions linked to the Project wherever stored, on their occurrence day (backfills included).

    Every row adds to ``totals["actions"]``; a row without a day adds to ``undated["actions"]``; a day inside ``series``
    adds to that day's ``"actions"``."""
    for row in uow.connection.execute("SELECT a.occurred_date FROM action_logs a JOIN action_businesses m "
                                      "ON m.action_key=a.action_key WHERE m.business_id=?", (business_id,)):
        totals["actions"] += 1
        if not row[0]: undated["actions"] += 1
        elif row[0] in series: series[row[0]]["actions"] += 1


def global_task_completions(uow, count):
    """Each Global Task first completed: ``count("tasks_done", completed_at)`` once per row."""
    for row in uow.connection.execute('SELECT completed_at FROM task_completions WHERE business_id IS NULL'):
        count('tasks_done', row[0])


def global_action_counts(uow, series):
    """Each canonical Action once, on its occurrence day, from the series' first day on; never a sum of Project counts."""
    for row in uow.connection.execute('SELECT occurred_date FROM action_logs WHERE occurred_date>=?', (min(series),)):
        if row[0] in series: series[row[0]]['actions'] += 1
