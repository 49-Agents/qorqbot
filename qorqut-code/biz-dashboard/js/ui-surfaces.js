// Keep native form controls and their events; use the app's popup for single selects.
(() => {
  const eligible = target => target instanceof HTMLSelectElement && !target.disabled && !target.multiple && target.size <= 1;
  function openSelect(select) {
    _tipHide();
    if (typeof writingTipHide === 'function') writingTipHide();
    select.focus({preventScroll:true});
    const options = [...select.options].map((option, index) => ({value:index,
      label:option.parentElement instanceof HTMLOptGroupElement ? `${option.parentElement.label} — ${option.label}` : option.label,
      disabled:option.disabled || option.hidden || option.parentElement.disabled}));
    let observe;
    const menu = dropdown(select, options, index => {
      if (!select.isConnected || select.disabled || options[index].disabled) return;
      if (select.selectedIndex === index) return;
      select.selectedIndex = index;
      select.dispatchEvent(new Event('input', {bubbles:true}));
      select.dispatchEvent(new Event('change', {bubbles:true}));
    }, select.selectedIndex, {className:'select-menu', onClose:()=>{select.setAttribute('aria-expanded','false');observe?.disconnect();}});
    select.setAttribute('aria-expanded','true');
    observe = new MutationObserver(records => {
      if (!select.isConnected || select.disabled || !menu.el.isConnected || records.some(record=>select.contains(record.target))) {
        menu.close(); select.setAttribute('aria-expanded','false'); observe.disconnect();
      }
    });
    observe.observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled']});
  }
  document.addEventListener('pointerdown', event => {
    if (!eligible(event.target) || event.button !== 0) return;
    event.preventDefault(); openSelect(event.target);
  });
  document.addEventListener('click', event => { if (eligible(event.target)) event.preventDefault(); });
  document.addEventListener('keydown', event => {
    if (!eligible(event.target) || !['Enter',' ','ArrowDown','ArrowUp'].includes(event.key)) return;
    event.preventDefault(); openSelect(event.target);
  });

  // Suppress browser title bubbles only while an element is hovered/focused.
  // Restore attributes afterwards so existing callers can keep reading/updating titles.
  let titled = null;
  const restore = () => {
    if (!titled) return;
    const el=titled; titled=null;
    if (!el.getAttribute('title')) el.setAttribute('title',el._appTitle || '');
    _tipHide();
  };
  const enter = event => {
    const el=event.target.closest?.('[title]');
    if (!el || el === titled || !el.getAttribute('title')) return;
    restore(); titled=el; el._appTitle=el.getAttribute('title'); el.setAttribute('title','');
    let custom=el;
    while(custom && !custom._tipBound && !custom.hasAttribute?.('data-tip')) custom=custom.parentElement;
    if (!custom) { tip(el,()=>esc(el._appTitle)); el._tipShow(); }
    else if (custom===el && el._tipShow) el._tipShow();
  };
  document.addEventListener('pointerover',enter);
  document.addEventListener('focusin',enter);
  document.addEventListener('pointerout',event=>{if(titled && titled.contains(event.target) && !titled.contains(event.relatedTarget))restore();});
  document.addEventListener('focusout',event=>{if(titled && titled.contains(event.target))restore();});
  new MutationObserver(() => {
    if (!titled) return;
    if (!titled.isConnected) { restore(); return; }
    const next=titled.getAttribute('title');
    if(next){titled._appTitle=next;titled.setAttribute('title','');if(titled._tipShow)titled._tipShow();}
  }).observe(document.body,{attributes:true,attributeFilter:['title'],subtree:true,childList:true});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'){restore();if(typeof writingTipHide==='function')writingTipHide();}});
  window.addEventListener('scroll',restore,true);
  window.addEventListener('resize',restore);
})();
