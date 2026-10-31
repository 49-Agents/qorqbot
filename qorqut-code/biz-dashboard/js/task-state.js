// Small shared controller for task status edits.  It keeps server observations
// and local intent separate so a slow list refresh cannot undo a click.
const _taskStateEntries = new Map();
const _taskStateListeners = new Set();

const _taskStateScope = scope => scope == null ? null : String(scope);
const _taskStateKey = (scope, id) => JSON.stringify([_taskStateScope(scope), String(id)]);
const _taskStatePath = scope => _taskStateScope(scope) == null
  ? "/root/operations"
  : `/biz/${encodeURIComponent(_taskStateScope(scope))}/operations`;
const _taskStateRevision = task => task && Number.isInteger(Number(task.revision)) && Number(task.revision) > 0 ? Number(task.revision) : null;

function _taskStateEntry(scope, id) {
  const normalized = _taskStateScope(scope), key = _taskStateKey(normalized, id);
  let entry = _taskStateEntries.get(key);
  if (!entry) {
    entry = {scope: normalized, id: String(id), confirmed: null, task: null, desired: undefined,
      inFlight: false, activeDesired: undefined, fetchPromise: null, waiters: [], error: null};
    _taskStateEntries.set(key, entry);
  }
  return entry;
}

function _taskStateProjected(entry) {
  if (!entry.confirmed) return entry.task || null;
  const projected = {...entry.confirmed};
  if (entry.desired !== undefined) projected.status = entry.desired;
  return projected;
}

function _taskStateSnapshot(entry) {
  return {task: _taskStateProjected(entry), pending: entry.desired === undefined ? null : entry.desired,
    error: entry.error || null};
}

function _taskStateEmit(entry) {
  const snapshot = _taskStateSnapshot(entry);
  for (const callback of [..._taskStateListeners]) {
    try { callback(entry.scope, entry.id, snapshot); } catch (error) { setTimeout(() => { throw error; }, 0); }
  }
}

function _taskStateInstall(entry, task) {
  if (!task || task.id == null) return false;
  const incomingRevision = _taskStateRevision(task), currentRevision = _taskStateRevision(entry.confirmed);
  // Revision-less observations are useful for first paint, but must not replace
  // a versioned observation. A lower revision is always stale.
  if (currentRevision != null && (incomingRevision == null || incomingRevision < currentRevision)) {
    // A compact newer Focus projection may lack list fields. Fill only missing
    // metadata from this older full row; never downgrade known fields or state.
    entry.confirmed = {...task, ...entry.confirmed};
    return false;
  }
  const incoming = entry.confirmed ? {...entry.confirmed, ...task} : {...task};
  // UI code often passes its already projected object back to Set. An equal
  // revision therefore cannot replace the confirmed status with local intent.
  if (entry.confirmed && incomingRevision === currentRevision && (currentRevision != null || entry.desired !== undefined)) incoming.status = entry.confirmed.status;
  entry.confirmed = incoming;
  entry.task = _taskStateProjected(entry);
  return true;
}

function taskStateObserve(scope, task) {
  if (!task || task.id == null) return task || null;
  const entry = _taskStateEntry(scope, task.id);
  const before = entry.confirmed;
  const installed = _taskStateInstall(entry, task);
  entry.task = _taskStateProjected(entry);
  // List/detail panes may observe the same task repeatedly. Notify only when
  // the confirmed version or its status really changed; pending intent is
  // always projected on top of the new observation.
  if (installed && (!before || _taskStateRevision(before) !== _taskStateRevision(entry.confirmed) ||
      before.status !== entry.confirmed.status)) {
    if (_taskStateRevision(before) == null || _taskStateRevision(entry.confirmed) > _taskStateRevision(before)) entry.error = null;
    _taskStateEmit(entry);
  }
  return entry.task;
}

function taskStateSnapshot(scope, id) {
  return _taskStateSnapshot(_taskStateEntry(scope, id));
}

function taskStateSubscribe(callback) {
  if (typeof callback !== "function") return () => {};
  _taskStateListeners.add(callback);
  return () => _taskStateListeners.delete(callback);
}

function _taskStateResolve(entry, value) {
  const waiters = entry.waiters.splice(0);
  for (const waiter of waiters) waiter(value);
}

async function _taskStateRead(entry) {
  if (!entry.fetchPromise) {
    const args = {task_id: entry.id};
    if (entry.scope == null) args.scope = "global";
    entry.fetchPromise = _taskStateRequest(`${_taskStatePath(entry.scope)}/tasks_get`, args).then(coreData).then(data => {
      const task = data && data.task;
      if (!task || String(task.id) !== entry.id || _taskStateRevision(task) == null) throw new Error("The server returned an invalid task.");
      _taskStateInstall(entry, task);
      entry.task = _taskStateProjected(entry);
      return task;
    }).finally(() => { entry.fetchPromise = null; });
  }
  return entry.fetchPromise;
}

function _taskStateRequest(path, body) {
  if (typeof AbortController !== "function") return post(path, body);
  const controller = new AbortController();
  let timer;
  const request = post(path, body, {signal: controller.signal});
  const timeout = new Promise((resolve, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      const error = new Error("The task request timed out; its result is unknown.");
      error.code = "request_timeout";
      reject(error);
    }, 15000);
  });
  return Promise.race([request, timeout]).finally(() => clearTimeout(timer));
}

async function _taskStateDrain(entry) {
  if (entry.inFlight) return;
  entry.inFlight = true;
  try {
    if (_taskStateRevision(entry.confirmed) == null) await _taskStateRead(entry);
  } catch (error) {
    entry.error = error;
    entry.desired = undefined;
    entry.task = _taskStateProjected(entry);
    entry.inFlight = false;
    _taskStateEmit(entry);
    _taskStateResolve(entry, _taskStateSnapshot(entry));
    return;
  }
  if (entry.desired === undefined || entry.desired === entry.confirmed?.status) {
    entry.desired = undefined;
    entry.task = _taskStateProjected(entry);
    entry.inFlight = false;
    _taskStateEmit(entry);
    _taskStateResolve(entry, _taskStateSnapshot(entry));
    return;
  }

  const desired = entry.desired;
  entry.activeDesired = desired;
  const args = {task_id: entry.id, changes: {status: desired}, expected_revision: _taskStateRevision(entry.confirmed), idempotency_key: newRequestId()};
  if (entry.scope == null) args.scope = "global";
  try {
    const result = await _taskStateRequest(`${_taskStatePath(entry.scope)}/tasks_update`, args);
    if (result?.status === "accepted" || result?.status === "signoff_required") {
      const error = new Error(result.status === "signoff_required"
        ? "Submitted for approval; state has not changed."
        : "Submitted awaiting confirmation; state has not changed.");
      error.code = result.status;
      throw error;
    }
    const data = coreData(result);
    if (!data || !data.task || String(data.task.id) !== entry.id || _taskStateRevision(data.task) == null) {
      throw new Error("The server returned an invalid updated task.");
    }
    _taskStateInstall(entry, data.task);
    entry.error = null;
    // The returned task is authoritative. Preserve only a newer click made
    // while this request was in flight.
    if (entry.desired === desired) entry.desired = undefined;
    entry.task = _taskStateProjected(entry);
    entry.inFlight = false;
    _taskStateEmit(entry);
    if (entry.desired !== undefined) return _taskStateDrain(entry);
    _taskStateResolve(entry, _taskStateSnapshot(entry));
  } catch (error) {
    // A failed HTTP response may represent a committed mutation. Reconcile
    // once, then stop: queued clicks must never be blindly replayed.
    try {
      const current = await _taskStateRead(entry);
      const matches = current && current.status === desired;
      const hadQueued = entry.desired !== desired;
      entry.desired = undefined;
      entry.error = matches && !hadQueued && error.code !== "signoff_required" ? null :
        (hadQueued && matches ? Object.assign(new Error("Latest change was not saved; try again."), {code: "queued_change_not_saved"}) : error);
      entry.task = _taskStateProjected(entry);
    } catch (reconcileError) {
      entry.desired = undefined;
      entry.error = error;
      entry.task = _taskStateProjected(entry);
    }
    entry.inFlight = false;
    _taskStateEmit(entry);
    _taskStateResolve(entry, _taskStateSnapshot(entry));
  }
}

function taskStateSet(scope, task, next) {
  if (!task || task.id == null) return Promise.resolve({task: task || null, pending: null, error: new Error("Task identity is required.")});
  const entry = _taskStateEntry(scope, task.id);
  _taskStateInstall(entry, task);
  entry.task = _taskStateProjected(entry);
  const desired = next && typeof next === "object" ? next.status : next;
  if (desired == null) return Promise.resolve(_taskStateSnapshot(entry));
  entry.error = null;
  entry.desired = desired;
  entry.task = _taskStateProjected(entry);
  _taskStateEmit(entry);
  const promise = new Promise(resolve => entry.waiters.push(resolve));
  _taskStateDrain(entry);
  return promise;
}
