const VERSION = 'two-etf-pwa-v6-risk-blend-20261023';
const STATIC = ['index.html','style.css','app.js','auth.js','signal-state.mjs','manifest.webmanifest','icon-192.png','icon-512.png','maskable-512.png','apple-touch-icon.png'];
const BASE = new URL('./', self.location.href);
const DATA = new URL('data.enc.json', BASE).href;
const SAFE = new Set(STATIC.map(p=>new URL(p,BASE).href));
SAFE.add(BASE.href);
self.addEventListener('install',event=>event.waitUntil(caches.open(VERSION).then(c=>c.addAll(STATIC))));
self.addEventListener('message',event=>{if(event.data?.type==='ACTIVATE_UPDATE') self.skipWaiting();});
self.addEventListener('activate',event=>event.waitUntil((async()=>{for(const key of await caches.keys()){if(key.startsWith('two-etf-pwa-')&&key!==VERSION)await caches.delete(key);}await self.clients.claim();})()));
self.addEventListener('fetch',event=>{
  if(event.request.method!=='GET')return;
  const url = new URL(event.request.url);url.search='';
  if(url.origin!==BASE.origin)return;
  if(url.href===DATA){
    event.respondWith((async()=>{
      const cache=await caches.open(VERSION);
      try{
        const response=await fetch(event.request);
        if(!response.ok||response.redirected||!response.headers.get('content-type')?.includes('json'))throw Error('Invalid response');
        const payload=await response.clone().json();
        if(payload.format!=='two-etf-encrypted-v1'||payload.algorithm!=='AES-GCM'||payload.iterations!==600000||typeof payload.ciphertext!=='string')throw Error('Invalid encrypted snapshot');
        await cache.put(DATA,response.clone());return response;
      }catch(error){const saved=await cache.match(DATA);if(saved){const h=new Headers(saved.headers);h.set('X-PWA-Offline','1');return new Response(await saved.blob(),{headers:h,status:200});}throw error;}
    })());return;
  }
  if(!SAFE.has(url.href))return;
  event.respondWith((async()=>{
    const cache=await caches.open(VERSION);
    try{const r=await fetch(event.request);if(r.ok&&!r.redirected)await cache.put(url.href,r.clone());if(r.ok)return r;throw Error('Unavailable');}
    catch(e){return await cache.match(url.href)||(url.href===BASE.href?await cache.match(new URL('index.html',BASE).href):null)||new Response('请联网完成首次加载。',{status:503,headers:{'Content-Type':'text/plain;charset=utf-8'}});}
  })());
});
