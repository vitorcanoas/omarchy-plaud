// Presentation adapter for Plaud Web. No API calls or synthetic Generate clicks.
(() => {
  if (location.origin !== 'https://web.plaud.ai') return;
  const roots = '.generate-options-dialog, .templates-selector-dialog';
  let seen = false, last = '', pending = false, generating = false;
  function send(state) {
    if (state === last) return;
    last = state;
    window.webkit.messageHandlers.selector.postMessage(state);
  }
  // Observe only: the official page issues the generation request itself.
  // Seeing it go out is what tells "Generate now" apart from Cancel, which
  // closes the very same dialog.
  function watch(url) {
    if (!generating && typeof url === 'string' && url.includes('/ai/transsumm/')) {
      generating = true;
      send('generating');
      return true;
    }
    return false;
  }
  // 'sent' once the request has been answered: the page may issue it only
  // after both dialogs are gone, so the window must outlive them.
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    if (watch(url)) this.addEventListener('loadend', () => send('sent'));
    return open.apply(this, arguments);
  };
  const nativeFetch = window.fetch;
  window.fetch = function (input) {
    const result = nativeFetch.apply(this, arguments);
    if (watch(typeof input === 'string' ? input : input && input.url)) result.finally(() => send('sent'));
    return result;
  };
  function update() {
    pending = false;
    if (location.pathname === '/login') { send('login'); return; }
    const visible = [...document.querySelectorAll(roots)].map(root => {
      const dialog = [...root.querySelectorAll('.el-dialog')].find(d => d.getClientRects().length);
      return dialog && {root, dialog};
    }).filter(Boolean);
    if (visible.length) {
      seen = true;
      const top = visible.find(v => v.root.matches('.templates-selector-dialog')) || visible[0];
      if (top.root.matches('.templates-selector-dialog')) {
        // The official templates card is sized from the viewport (width and
        // height in vw/vh). Reporting that size would make the window follow
        // the card that follows the window: a resize loop that shrank the
        // picker to nothing and snapped it back (measured). The window gets
        // its fixed size and generation.css makes the card fill it.
        send('templates');
        return;
      }
      // scrollHeight is the card's natural height even while max-height
      // clamps it to the current window, so the window can grow to fit it.
      const r = top.dialog.getBoundingClientRect();
      send('ready:' + Math.round(r.width) + ':' + Math.round(Math.max(r.height, top.dialog.scrollHeight)));
    } else if (seen) {
      send(generating ? 'generating' : 'closed');
    }
  }
  new MutationObserver(() => {
    if (!pending) { pending = true; requestAnimationFrame(update); }
  }).observe(document.documentElement, {subtree:true,childList:true,attributes:true,attributeFilter:['style','class']});
  addEventListener('DOMContentLoaded', update);
  addEventListener('resize', update);
  addEventListener('load', () => { update(); if (!seen) send('page'); });
})();
