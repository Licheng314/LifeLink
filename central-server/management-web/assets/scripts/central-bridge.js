/* Keep the copied PC DOM/scripts intact while the central endpoint adapter is
   introduced.  This only adds the local management CSRF proof to same-origin
   mutations; it never adds credentials or changes cross-origin requests. */
(() => {
  const csrf = document.querySelector('meta[name="lifelink-csrf"]')?.content;
  if (!csrf) return;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const requestUrl = new URL(typeof input === 'string' ? input : input.url, window.location.href);
    const method = String(init.method || (typeof input === 'string' ? 'GET' : input.method) || 'GET').toUpperCase();
    if (requestUrl.origin !== window.location.origin || method === 'GET' || method === 'HEAD') return nativeFetch(input, init);
    const headers = new Headers(init.headers || (typeof input === 'string' ? undefined : input.headers));
    headers.set('X-CSRF-Token', csrf);
    return nativeFetch(input, {...init, headers});
  };
})();
