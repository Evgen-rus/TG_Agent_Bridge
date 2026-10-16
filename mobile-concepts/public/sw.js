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
self.addEventListener('push',event=>{
 if(!new URL(self.registration.scope).pathname.startsWith('/rick/'))return;
 let data;try{data=event.data?.json()}catch{return}
 if(!data||typeof data!=='object')return;
 const url=new URL(typeof data.url==='string'?data.url:'/rick/',self.location.origin);
 if(url.origin!==self.location.origin||url.pathname!=='/rick/')return;
 event.waitUntil(self.registration.showNotification('Рик',{
  body:data.body==='Время напоминания. Откройте Рика.'?data.body:'Ответ Рика готов. Откройте приложение.',
  tag:typeof data.tag==='string'?data.tag:'rick',data:{url:url.href},icon:'/rick/icon.svg',
 }));
});
self.addEventListener('notificationclick',event=>{
 event.notification.close();
 const url=new URL(event.notification.data?.url||'/rick/',self.location.origin);
 if(url.origin!==self.location.origin||url.pathname!=='/rick/')return;
 event.waitUntil((async()=>{
  const clients=await self.clients.matchAll({type:'window',includeUncontrolled:true});
  const client=clients.find(item=>new URL(item.url).origin===url.origin&&new URL(item.url).pathname.startsWith('/rick/'));
  if(client){await client.navigate(url.href);await client.focus()}else await self.clients.openWindow(url.href);
 })());
});
