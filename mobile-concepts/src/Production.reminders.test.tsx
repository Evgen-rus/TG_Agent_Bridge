import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';

afterEach(()=>{cleanup();vi.unstubAllGlobals();window.history.replaceState(null,'','/')});
it('opens the notification context, confirms a reminder and offers the Rick Telegram link',async()=>{
 vi.resetModules();window.history.replaceState(null,'','/rick/?context=202&reminder=10');
 const {default:Production}=await import('./Production');
 let confirmed=false;
 const fetch=vi.fn(async(input:string,options?:RequestInit)=>{
  const route=input.split('/api/')[1];let value:unknown={};
  if(route==='session')value={name:'Owner',role:'owner',login:'evgen',csrf:'csrf'};
  else if(route==='contexts')value=[{id:'101',title:'First'},{id:'202',title:'Chosen client'}];
  else if(route==='telegram')value={url:'https://t.me/c/777/'};
  else if(route==='reminder?id=10')value={id:10,action:'Сделать аналитику',remind_at_utc:'2099-10-17T04:00:00Z'};
  else if(route.startsWith('detail'))value={id:'202',title:'Chosen client',events:[],recommendations:[],facts:[],commitments:[],reviews:[]};
  else if(route.startsWith('history'))value=[{id:15,payload:'{"text":"Напомни завтра"}',state:confirmed?'done':'review',result:confirmed?'Напоминание сохранено':'Подтвердите напоминание',created:1800000000,task_id:18,revision:2,can_confirm:1}];
  else if(route==='reminder-action'){
   expect(JSON.parse(options?.body as string)).toEqual({context:'202',job_id:15,revision:2,action:'confirm'});
   confirmed=true;value={id:15};
  }
  return new Response(JSON.stringify(value),{status:200});
 });
 vi.stubGlobal('fetch',fetch);render(<Production/>);
 expect(await screen.findByRole('heading',{name:'Сделать аналитику'})).toBeTruthy();
 fireEvent.click(await screen.findByRole('button',{name:'Подтвердить напоминание'}));
 expect(await screen.findByText('Напоминание сохранено')).toBeTruthy();
 expect((screen.getByRole('combobox',{name:'Контекст'}) as HTMLSelectElement).value).toBe('202');
 fireEvent.click(screen.getByRole('button',{name:'Ещё'}));
 await waitFor(()=>expect(screen.getByRole('link',{name:'Перейти в Telegram'}).getAttribute('href')).toBe('https://t.me/c/777/'));
 expect(screen.getByText('Уведомления Рика')).toBeTruthy();
});

it.each(['rick','sally'])('reports foreground presence and shows due reminder inside matching %s chat',async(product)=>{
 vi.resetModules();window.history.replaceState(null,'',`/${product}/?context=202`);
 const {default:Production}=await import('./Production');let delivered=false;
 const fetch=vi.fn(async(input:string)=>{
  const route=input.split('/api/')[1];let value:unknown={};
  if(route==='session')value={name:'Owner',role:'owner',login:'evgen',csrf:'csrf'};
  else if(route==='contexts')value=[{id:'202',title:'Chosen client'}];
  else if(route.startsWith('detail'))value={id:'202',title:'Chosen client',events:[],recommendations:[],facts:[],commitments:[],reviews:[]};
  else if(route.startsWith('history'))value=[];
  else if(route.startsWith('notifications')){value={notifications:delivered?[]:[{context:'202',reminder_id:10}],cursor:Date.now()/1000};delivered=true}
  else if(route==='reminder?id=10')value={id:10,action:'Проверить договор',remind_at_utc:'2099-10-17T04:00:00Z'};
  return new Response(JSON.stringify(value),{status:200});
 });
 vi.stubGlobal('fetch',fetch);render(<Production/>);
 expect(await screen.findByRole('alert')).toBeTruthy();expect(screen.getByText('Проверить договор')).toBeTruthy();
 expect(fetch).toHaveBeenCalledWith(`/${product}/api/presence`,expect.objectContaining({keepalive:true,body:expect.stringContaining('"visible":true')}));
 fireEvent.click(screen.getByRole('button',{name:'Ещё'}));expect(screen.queryByText('Проверить договор')).toBeNull();
 Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});fireEvent(document,new Event('visibilitychange'));
 expect(fetch).toHaveBeenCalledWith(`/${product}/api/presence`,expect.objectContaining({body:expect.stringContaining('"visible":false')}));
 Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
});
it('Sally review uses the shared confirmation controls',async()=>{
 vi.resetModules();window.history.replaceState(null,'','/sally/?context=0');
 const {default:Production}=await import('./Production');let confirmed=false;
 vi.stubGlobal('fetch',vi.fn(async(input:string,options?:RequestInit)=>{
  const route=input.split('/api/')[1];let value:unknown={};
  if(route==='session')value={name:'Owner',role:'owner',login:'evgen',csrf:'csrf'};
  else if(route==='contexts')value=[{id:'0',title:'General'}];
  else if(route.startsWith('detail'))value={id:'0',title:'General',events:[],recommendations:[],facts:[],commitments:[],reviews:[]};
  else if(route.startsWith('history'))value=confirmed?[{id:1,task_id:2,state:'done',result:'Напоминание Салли сохранено'}]:[{id:1,task_id:2,state:'review',revision:1,can_confirm:1,result:'Завтра в 12:00 — проверить',payload:'{}'}];
  else if(route==='reminder-action'){expect(JSON.parse(options?.body as string)).toEqual({context:'0',job_id:1,revision:1,action:'confirm'});confirmed=true}
  return new Response(JSON.stringify(value),{status:200});
 }));
 render(<Production/>);fireEvent.click(await screen.findByRole('button',{name:'Подтвердить напоминание'}));
 await waitFor(()=>expect(confirmed).toBe(true));expect(await screen.findByText('Ваш запрос #1: Напоминание Салли сохранено')).toBeTruthy();
});
