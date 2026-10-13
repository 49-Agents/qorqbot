import type { Result } from './wire';

export class ApiError extends Error {
  constructor(public code: string, message: string, public ambiguous = false, public status = 0, public retryable?: boolean) {
    super(message);
  }
}

// Request bodies stay in page memory. Local access sends no browser credentials.
export async function request<T>(path: string, init: RequestInit = {}, mutation = false): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  init.signal?.addEventListener('abort', abort, { once: true });
  if (init.signal?.aborted) controller.abort();
  const timer = window.setTimeout(abort, 15000);
  try {
    const response = await fetch(path, {
      ...init, credentials: 'omit', cache: 'no-store', redirect: 'error',
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...init.headers },
    });
    if (response.status === 401) throw new ApiError('transport_refused', 'Request refused (HTTP 401).', false, 401);
    let body: unknown;
    try { body = await response.json(); }
    catch { throw new ApiError('invalid_response', 'The server returned an unreadable response.', mutation, response.status); }
    // A transport refusal has no application receipt. Do not infer rollback from a server failure.
    if (!response.ok && !(body && typeof body === 'object' && 'ok' in body)) {
      throw new ApiError('transport_refused', `Request refused (HTTP ${response.status}).`, mutation && response.status >= 500, response.status);
    }
    return body as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError('connection_lost', mutation
      ? 'Confirmation was lost. This request may have been saved.'
      : 'Cannot reach the server. Your drafts are kept on this page.', mutation);
  } finally {
    clearTimeout(timer);
    init.signal?.removeEventListener('abort', abort);
  }
}

export async function operation<T>(name: string, args: object, signal?: AbortSignal, mutation = false): Promise<T> {
  const result = await request<Result<T>>(`/api/v1/operations/${name}`, {
    method: 'POST', body: JSON.stringify(args), signal,
  }, mutation);
  if (typeof result?.ok !== 'boolean') throw new ApiError('invalid_response', 'Missing application receipt.', mutation);
  if (!result.ok) {
    throw new ApiError(result.error?.code || 'invalid_response', result.error?.message || 'Request failed.', result.error?.code === 'uncertain_effect', 0,
      typeof result.error?.retryable === 'boolean' ? result.error.retryable : undefined);
  }
  if (!result.data || !result.outcome) throw new ApiError('invalid_response', 'Incomplete application receipt.', mutation);
  return result.data;
}
