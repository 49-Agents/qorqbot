// Shared label picker for Tasks and Actions. Tags are plain labels from one installation-wide catalog
// (`tags_list`). A label that is not in the catalog yet is created simply by applying it, so there is no
// separate "create tag" step. Tags never grant access, and there is no rename or merge.
const TAG_MAX_LENGTH = 64, TAG_MAX_COUNT = 100;

function tagValid(label) {
  return label.length >= 1 && label.length <= TAG_MAX_LENGTH && !/\p{C}/u.test(label);
}
const tagKey = label => label.normalize('NFKC').toLocaleLowerCase();
function tagNormalize(labels) {
  const seen = new Set(), out = [];
  for (const raw of labels || []) {
    const label = String(raw).trim().replace(/\s+/g, ' ');
    if (!tagValid(label) || seen.has(tagKey(label))) continue;
    seen.add(tagKey(label)); out.push(label);
  }
  return out.slice(0, TAG_MAX_COUNT);
}

async function tagCatalogSearch(biz, query) {
  const data = await workCall(biz, 'tags_list', {query: query.slice(0, TAG_MAX_LENGTH), limit: 20});
  return (data.tags || []).map(tag => tag.label);
}

// Mounts chips plus a typeahead into `host`. `onChange(labels)` runs after every add/remove; the caller
// decides when the labels are written. Returns {labels(), set(labels)}.
function tagPickerMount(host, {biz = null, labels = [], onChange = null, disabled = false, label = 'Tags'} = {}) {
  let value = tagNormalize(labels), generation = 0, timer = null;
  host.classList.add('tag-picker');
  host.innerHTML = `<div class="tag-picker-chips" aria-label="${esc(label)}"></div>
    <input class="inp tag-picker-input" type="text" maxlength="${TAG_MAX_LENGTH}" autocomplete="off" spellcheck="false" aria-label="Add a tag" placeholder="Add a tag"${disabled ? ' disabled' : ''}>
    <div class="tag-picker-menu" role="listbox" hidden></div><p class="tag-picker-error ferr" role="alert" hidden></p>`;
  const chips = host.querySelector('.tag-picker-chips'), input = host.querySelector('input'), menu = host.querySelector('.tag-picker-menu'), error = host.querySelector('.tag-picker-error');
  const emit = () => onChange?.(value.slice());
  const paintChips = () => {
    chips.replaceChildren();
    for (const tag of value) {
      const chip = document.createElement('span'); chip.className = 'chip tag-chip';
      const text = document.createElement('span'); text.textContent = tag;
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'quiet tag-remove';
      remove.setAttribute('aria-label', `Remove tag ${tag}`); remove.textContent = '×'; remove.disabled = disabled;
      remove.onclick = () => { value = value.filter(item => item !== tag); paintChips(); emit(); };
      chip.append(text, remove); chips.append(chip);
    }
  };
  const closeMenu = () => { menu.hidden = true; menu.replaceChildren(); };
  const add = text => {
    const [next] = tagNormalize([text]);
    if (!next) { error.textContent = `A tag is a visible label of 1 to ${TAG_MAX_LENGTH} characters.`; error.hidden = false; return; }
    error.hidden = true; input.value = ''; closeMenu();
    if (value.some(item => tagKey(item) === tagKey(next))) return;
    if (value.length >= TAG_MAX_COUNT) { error.textContent = `At most ${TAG_MAX_COUNT} tags.`; error.hidden = false; return; }
    value = [...value, next]; paintChips(); emit();
  };
  const suggest = async () => {
    const own = ++generation, query = input.value.trim();
    let found = [];
    try { found = await tagCatalogSearch(biz, query); error.hidden = true; }
    catch (failure) { if (own === generation && host.isConnected) { error.textContent = 'Tag catalog unavailable: ' + failure.message; error.hidden = false; } }
    if (own !== generation || !host.isConnected) return;
    found = found.filter(item => !value.some(have => tagKey(have) === tagKey(item)));
    const exact = query && (found.some(item => tagKey(item) === tagKey(query)) || value.some(item => tagKey(item) === tagKey(query)));
    const options = [...found.map(item => ({label: item, create: false})), ...(query && !exact && tagValid(query) ? [{label: query, create: true}] : [])];
    menu.replaceChildren();
    for (const option of options) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'quiet tag-option'; button.setAttribute('role', 'option');
      button.textContent = option.create ? `Create “${option.label}”` : option.label; button.dataset.tag = option.label;
      button.onmousedown = event => event.preventDefault();
      button.onclick = () => add(option.label);
      menu.append(button);
    }
    menu.hidden = !options.length;
  };
  input.oninput = () => { clearTimeout(timer); timer = setTimeout(suggest, 150); };
  input.onfocus = () => suggest();
  input.onblur = () => setTimeout(closeMenu, 150);
  input.onkeydown = event => {
    if (event.key === 'Enter') { event.preventDefault(); if (input.value.trim()) add(input.value); }
    else if (event.key === 'Escape' && !menu.hidden) { event.stopPropagation(); closeMenu(); }
  };
  paintChips();
  return {labels: () => value.slice(), set: next => { value = tagNormalize(next); paintChips(); }};
}
