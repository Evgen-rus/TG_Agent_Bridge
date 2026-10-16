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
