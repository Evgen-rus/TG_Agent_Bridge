import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {expect,it,vi} from 'vitest';

const source=readFileSync('public/sw.js','utf8');
function worker(product='rick'){
 const listeners:Record<string,(event:unknown)=>void>={};
 const client={url:`https://example.test/${product}/`,navigate:vi.fn(async()=>{}),focus:vi.fn(async()=>{})};
 const showNotification=vi.fn(async()=>{});
 const self={registration:{scope:`https://example.test/${product}/`,showNotification},location:{origin:'https://example.test'},
  addEventListener:(type:string,callback:(event:unknown)=>void)=>{listeners[type]=callback},
  clients:{matchAll:async()=>[client],openWindow:vi.fn(async()=>{})}};
 runInNewContext(source,{self,URL});
 return {listeners,client,showNotification};
}
it('service worker shows a generic reminder and opens the existing Rick context',async()=>{
 const {listeners,client,showNotification}=worker();let pending:Promise<unknown>|undefined;
 listeners.push({data:{json:()=>({body:'Время напоминания. Откройте Рика.',url:'/rick/?context=202',tag:'event'})},waitUntil:(p:Promise<unknown>)=>{pending=p}});
 await pending;
 expect(showNotification).toHaveBeenCalledWith('Рик',expect.objectContaining({body:'Время напоминания. Откройте Рика.',tag:'event',icon:'/rick/rick-v2-192.png'}));
 listeners.notificationclick({notification:{close:vi.fn(),data:{url:'https://example.test/rick/?context=202'}},waitUntil:(p:Promise<unknown>)=>{pending=p}});
 await pending;
 expect(client.navigate).toHaveBeenCalledWith('https://example.test/rick/?context=202');expect(client.focus).toHaveBeenCalledOnce();
});
it('does not display Rick push in Sally or navigate to an external URL',()=>{
 const sally=worker('sally');
 sally.listeners.push({data:{json:()=>({url:'/rick/'})},waitUntil:vi.fn()});
 expect(sally.showNotification).not.toHaveBeenCalled();
 const rick=worker();const waitUntil=vi.fn();
 rick.listeners.notificationclick({notification:{close:vi.fn(),data:{url:'https://evil.test/rick/'}},waitUntil});
 expect(waitUntil).not.toHaveBeenCalled();
});

it('Sally worker displays its own reminders and focuses only Sally',async()=>{
 const {listeners,client,showNotification}=worker('sally');let pending:Promise<unknown>|undefined;
 listeners.push({data:{json:()=>({body:'Время напоминания. Откройте Сали.',url:'/sally/?context=0&reminder=5',tag:'private-hash'})},waitUntil:(p:Promise<unknown>)=>{pending=p}});
 await pending;
 expect(showNotification).toHaveBeenCalledWith('Салли',expect.objectContaining({body:'Время напоминания. Откройте Салли.',icon:'/sally/sally-v2-192.png'}));
 listeners.notificationclick({notification:{close:vi.fn(),data:{url:'https://example.test/sally/?context=0&reminder=5'}},waitUntil:(p:Promise<unknown>)=>{pending=p}});
 await pending;
 expect(client.navigate).toHaveBeenCalledWith('https://example.test/sally/?context=0&reminder=5');
});
