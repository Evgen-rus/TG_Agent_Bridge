// Only immutable build assets enter the cache; no pages, API, cookies or client history.
const PREFIX='agentbridge-static-';
const CACHE=PREFIX+'v1-'+self.registration.scope;
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>{
 const url=new URL(event.request.url);
 if(event.request.method!=='GET'||url.origin!==self.location.origin||!url.pathname.startsWith(new URL(self.registration.scope).pathname+'assets/'))return;
 event.respondWith(caches.open(CACHE).then(async cache=>{const cached=await cache.match(event.request);if(cached)return cached;const response=await fetch(event.request);if(response.ok&&response.type==='basic')await cache.put(event.request,response.clone());return response}));
});
