// Original local line artwork. Keys are the fixed core v1 catalog.
export const iconPaths: Record<string, string> = {
  circle: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  square: 'M4 4h16v16H4Z',
  triangle: 'm12 3 10 18H2Z',
  diamond: 'm12 2 10 10-10 10L2 12Z',
  star: 'm12 2 3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1Z',
  compass: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-5-4-3 5-5 3 3-5Z',
  globe: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM3 12h18M12 3c-5 4-5 14 0 18 5-4 5-14 0-18Z',
  leaf: 'M20 3C8 2 1 9 6 17c8 5 15-2 14-14ZM3 21 16 8',
  feather: 'M3 21 16 8M7 17C2 9 11 1 18 3c7 5-1 14-9 14Zm3-3h6M12 12V7',
  book: 'M12 5C8 2 4 3 2 4v16c3-2 7-2 10 0 3-2 7-2 10 0V4c-2-1-6-2-10 1Zm0 0v15',
  notebook: 'M6 3h14v18H6ZM3 7h5M3 12h5M3 17h5M11 7h5M11 11h5',
  flask: 'M9 3h6M10 3v7L4 20c0 1 16 1 16 0l-6-10V3M7 15h10',
  microscope: 'm10 3 4-1 4 8-4 2Zm3 9-2 1-2-4M6 21h14M13 14v7M18 12c7 6-1 10-8 7M4 16h6',
  telescope: 'm3 10 14-6 3 7-15 4Zm14-6 2-1 3 7-2 1M12 13v8M12 15l-5 6M12 15l5 6M3 10l-2 1 2 5 2-1',
  code: 'm8 6-6 6 6 6M16 6l6 6-6 6M14 3l-4 18',
  terminal: 'M3 4h18v16H3Zm3 4 4 4-4 4M13 16h5',
  wrench: 'M14 3a6 6 0 0 0-6 8l-6 7 4 4 7-7a6 6 0 0 0 8-6l-4 3-4-4Z',
  gear: 'm9 3 1-1h4l1 3 3 1 3-1 2 4-2 2v3l2 2-2 4-3-1-3 1-1 2h-4l-1-3-3-1-3 1-2-4 2-2v-3L1 8l2-4 3 1 3-1ZM16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  lightbulb: 'M9 17c0-4-4-4-4-9a7 7 0 0 1 14 0c0 5-4 5-4 9ZM9 20h6M10 23h4M12 17v-6',
  anchor: 'M14 4a2 2 0 1 1-4 0 2 2 0 0 1 4 0ZM12 6v15M7 9h10M3 14v3c3 6 15 6 18 0v-3M1 16l2-2 2 2M19 16l2-2 2 2',
};
export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={iconPaths[name] || iconPaths.circle} /></svg>;
}
export function IconPicker({ value, onChange, keys, disabled = false }: { value: string; onChange: (key: string) => void; keys: string[]; disabled?: boolean }) {
  return <fieldset className="icon-picker" disabled={disabled}><legend>Icon</legend>
    <div className="icon-grid">{keys.filter(key => key in iconPaths).map(key => <label title={key} key={key}>
      <input type="radio" name="icon" value={key} checked={value === key} onChange={() => onChange(key)} aria-label={key} />
      <span><Icon name={key} size={23} /></span>
    </label>)}</div>
  </fieldset>;
}
