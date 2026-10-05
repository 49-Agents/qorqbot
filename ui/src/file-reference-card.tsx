import { useState } from 'preact/hooks';
import type { FileReference } from './wire';

export function FileReferenceCard({ reference }: { reference: FileReference }) {
  const [feedback, setFeedback] = useState('');
  async function copyPath() {
    try {
      await navigator.clipboard.writeText(reference.path);
      setFeedback('Copied');
    } catch {
      setFeedback('Could not copy. Select and copy the path manually.');
    }
  }
  return <section className="file-card" aria-label="File reference">
    <div className="file-card-title">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" aria-hidden="true"><path d="M14 3H5v18h14V8zM14 3v5h5" /></svg>
      <strong>{reference.label}</strong>
    </div>
    <p className="file-location">{reference.location === 'workspace' ? 'Agent workspace' : 'Host path'}</p>
    <p className="file-path">{reference.path}</p>
    {reference.note !== null && reference.note !== '' && <p className="file-note">{reference.note}</p>}
    <p className="file-size">{Number.isSafeInteger(reference.size_bytes)
      ? `Observed size: ${reference.size_bytes.toLocaleString()} bytes`
      : 'Size exceeds precise display range'}</p>
    <div className="file-actions"><button type="button" onClick={() => void copyPath()}>Copy path</button><span role="status">{feedback}</span></div>
  </section>;
}
