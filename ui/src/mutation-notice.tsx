import type { Store, Pending } from './store';

export function MutationNotice({ store, item, requestKey }: { store: Store; item?: Pending; requestKey: string }) {
  if (!item) return null;
  if (item.state === 'sending') return <p className="quiet" role="status">Saving request…</p>;
  return <div className="error mutation-error" role="alert">
    <p>{item.unresolvedAmbiguity ? 'Confirmation uncertain. ' : ''}{item.error}</p>
    {item.operation === 'agents_update' && <p>Attempted {item.args.display_name !== undefined ? `name: “${item.args.display_name}”` : `icon: ${item.args.icon_key}`}.</p>}
    {item.actor !== store.actor ? <p>This request belongs to a different workspace identity. Reconnect to the original workspace to retry it.</p> : <button type="button" onClick={() => void store.retry(requestKey)} disabled={!store.has(item.operation)}>
      {!item.unresolvedAmbiguity && item.state === 'conflict' ? 'Apply attempted change to current revision' : 'Retry original request'}
    </button>}
    {!item.unresolvedAmbiguity && (item.state === 'failed' || item.state === 'conflict') && <button type="button" onClick={() => store.discard(requestKey)}>Dismiss error</button>}
    {item.unresolvedAmbiguity && <p className="quiet">Retry keeps the exact request and idempotency key.</p>}
  </div>;
}
