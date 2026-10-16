import {it,expect,vi,afterEach} from 'vitest';
import {render,screen,waitFor,fireEvent} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Production from './Production';

afterEach(()=>vi.unstubAllGlobals());

it('реальный UI: вход, контекст, ошибка отправки и повтор с тем же idempotency key',async()=>{
 let authenticated=false,attempt=0,completed=false;
 const contexts=[{id:'101',title:'Рабочий контекст'}];
 const keys:string[]=[];
 const fetchMock=vi.fn(async(input:string,options?:RequestInit)=>{
  const route=input.split('/api/')[1];
  let value:unknown={},status=200;
  if(route==='session'){
   if(authenticated)value={name:'Евгений',login:'evgen',role:'owner',csrf:'csrf-test'};
   else {status=401;value={error:'Войдите'}}
  }else if(route==='login'){authenticated=true}
  else if(route==='contexts')value=contexts;
  else if(route.startsWith('detail'))value={id:'101',title:'Рабочий контекст',events:[],recommendations:[],facts:[],commitments:[],reviews:[]};
  else if(route.startsWith('history'))value=completed?[{id:1,payload:JSON.stringify({text:'Мой вопрос'}),state:'done',result:'Готовый ответ',created:1800000000}]:[];
  else if(route==='ask'){
   const headers=options?.headers as Record<string,string>;
   keys.push(headers['Idempotency-Key']);
   expect(headers['X-CSRF-Token']).toBe('csrf-test');
   expect(JSON.parse(options?.body as string)).toEqual({context:'101',text:'Мой вопрос'});
   if(attempt++===0){status=503;value={error:'Сеть недоступна'}}else{completed=true;status=202;value={id:1}}
  }
  return new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
 });
 vi.stubGlobal('fetch',fetchMock);
 const u=userEvent.setup();render(<Production/>);
 await u.type(await screen.findByLabelText('Логин'),'evgen');
 await u.type(screen.getByLabelText('Пароль'),'safe-test-password');
 await u.click(screen.getByRole('button',{name:'Войти'}));
 await u.click(await screen.findByRole('button',{name:/Рабочий контекст/}));
 await u.click(await screen.findByRole('button',{name:'Обсудить с Риком'}));
 await u.type(screen.getByRole('textbox',{name:'Сообщение'}),'Мой вопрос');
 await u.click(screen.getByRole('button',{name:'Отправить вопрос'}));
 expect(await screen.findByText('Сеть недоступна')).toBeTruthy();
 await u.click(screen.getByRole('button',{name:'Отправить вопрос'}));
 expect(await screen.findByText('Готовый ответ')).toBeTruthy();
 expect(keys.length).toBe(2);
 expect(keys[0]).toBe(keys[1]);
 await u.click(screen.getByRole('button',{name:'Скопировать'}));
 await waitFor(()=>expect(screen.getByText('Скопировано. Отправка остаётся за вами.')).toBeTruthy());
 contexts.push({id:'102',title:'Новый клиент'});
 fireEvent(window,new Event('focus'));
 await u.click(screen.getByRole('button',{name:'События'}));
 expect(await screen.findByRole('button',{name:/Новый клиент/})).toBeTruthy();
});
