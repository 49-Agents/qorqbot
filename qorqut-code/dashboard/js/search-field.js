// The one search bar. searchField({placeholder, label, value, attrs, className}) returns the HTML of
// <label class="search-field">: the search icon and an <input type="search">. `label` is the input's aria-label
// (it defaults to the placeholder); `attrs` is an object of extra attributes on the input (id, data-*, ...);
// `className` adds classes to the label. Look and size live in css/search-field.css.
const SEARCH_FIELD_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>`;
function searchField({placeholder = '', label = '', value = '', attrs = {}, className = ''} = {}) {
  const esc = text => String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const aria = label || placeholder;
  const extra = Object.entries(attrs).map(([name, val]) => val === true || val === '' ? ` ${name}` : ` ${name}="${esc(val)}"`).join('');
  return `<label class="search-field${className ? ' ' + esc(className) : ''}">${SEARCH_FIELD_ICON}<input type="search"${placeholder ? ` placeholder="${esc(placeholder)}"` : ''}${aria ? ` aria-label="${esc(aria)}"` : ''}${value ? ` value="${esc(value)}"` : ''} autocomplete="off" spellcheck="false"${extra}></label>`;
}
