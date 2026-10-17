import {act,fireEvent,render,screen,waitFor,cleanup} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';

afterEach(()=>{cleanup();vi.useRealTimers();vi.unstubAllGlobals()});

it.each(['rick','sally'])('%s shows only animated dots while working and removes them on completion/error/review',async product=>{
 vi.resetModules();window.history.replaceState(null,'',`/${product}/`);
 const {default:Production}=await import('./Production');
 let state='running';
 vi.stubGlobal('fetch',vi.fn(async(input:string)=>{
  const route=input.split('/api/')[1];let value:unknown={};
  if(route==='session')value={name:'Test',role:'owner',login:'test',csrf:'fake'};
  else if(route==='contexts')value=[{id:'0',title:'General'}];
  else if(route.startsWith('detail'))value={id:'0',title:'General',events:state==='telegram'?[{id:2,text:'Вопрос из Telegram',author:'Test',processed:0}]:[],messages:state==='done'?[{id:1,key:'answer',author:'Салли',text:'Готовый ответ'}]:[],recommendations:[],facts:[],commitments:[],reviews:[]};
  else if(route.startsWith('history'))value=state==='telegram'?[]:[{id:1,author:'Test',payload:'{"text":"Question"}',state,result:state==='done'?'Готовый ответ':state==='error'?'Ошибка обработки':state==='review'?'Подтвердите в Telegram':'',created:1800000000}];
  return new Response(JSON.stringify(value),{status:200});
 }));
 render(<Production/>);
 expect(document.querySelector('link[rel="apple-touch-icon"]')?.getAttribute('href')??null).toBe(product==='sally'?'/sally/sally-v2-180.png':null);
 expect(document.querySelector('link[rel="icon"]')?.getAttribute('href')??null).toBe(product==='sally'?'/sally/sally-v2-192.png':null);
 if(product==='sally')fireEvent.click(await screen.findByRole('button',{name:'General · общий чат'}));
 else fireEvent.click(await screen.findByRole('button',{name:'Диалог'}));
 const indicator=await screen.findByRole('status',{name:'Идёт обработка'});
 expect(indicator.textContent).toBe('');expect(indicator.querySelectorAll('i')).toHaveLength(3);
 expect(screen.queryByText('Обрабатывается')).toBeNull();
 vi.useFakeTimers();
 fireEvent.click(screen.getByRole('button',{name:'К карточке'}));
 fireEvent.click(screen.getByRole('button',{name:`Обсудить с ${product==='rick'?'Риком':'Салли'}`}));
 for(const next of ['error','waiting','review','running','done',...(product==='sally'?['telegram','done']:[])]){
  state=next;
  await act(async()=>{await vi.advanceTimersByTimeAsync(2500)});
  if(['waiting','running','telegram'].includes(next))expect(screen.getByRole('status',{name:'Идёт обработка'})).toBeTruthy();
  else expect(screen.queryByRole('status',{name:'Идёт обработка'})).toBeNull();
  if(next==='error')expect(screen.getByText(/Ошибка обработки/)).toBeTruthy();
  if(next==='review')expect(screen.getByText(/Подтвердите в Telegram/)).toBeTruthy();
 }
 vi.useRealTimers();
 await waitFor(()=>expect(screen.getByText('Готовый ответ')).toBeTruthy());
});
