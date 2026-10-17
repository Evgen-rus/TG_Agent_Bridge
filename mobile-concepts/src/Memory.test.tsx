import {it,expect,vi} from 'vitest';
import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {Memory} from './Memory';

const rule={id:1,title:'Короткий ответ',situation:'Клиент просит итог',behavior:'Начать с результата',rationale:'Исправление команды',scope:'global',context:'',status:'pending',version:2,sources:[{text:'Обсуждение владельца',author:'Евгений',url:'javascript:alert(1)',date:1800000000}]};
it('показывает источники и подтверждает точную версию',async()=>{
 const api=vi.fn().mockResolvedValueOnce({candidates:[rule],rules:[],history:[]}).mockResolvedValueOnce({candidates:[],rules:[{...rule,status:'active'}],history:[]});
 const user=userEvent.setup();render(<Memory api={api}/>);
 expect(await screen.findByText('Короткий ответ')).toBeTruthy();
 await user.click(screen.getByText('Источники (1)'));
 expect(screen.getByText('Обсуждение владельца')).toBeTruthy();
 expect(screen.queryByRole('link')).toBeNull();
 await user.click(screen.getByRole('button',{name:'Принять'}));
 expect(api).toHaveBeenLastCalledWith('memory',{id:1,action:'accept',expected_version:2});
 await user.click(screen.getByRole('button',{name:'Действующие (1)'}));
 expect(await screen.findByRole('button',{name:'Отключить'})).toBeTruthy();
});
it('уточнение сохраняет редакцию и отдельно требует принятия',async()=>{
 const api=vi.fn().mockResolvedValue({candidates:[rule],rules:[],history:[]});
 const user=userEvent.setup();render(<Memory api={api}/>);
 await user.click(await screen.findByRole('button',{name:'Уточнить'}));
 await user.clear(screen.getByRole('textbox',{name:'Что делать иначе'}));
 await user.type(screen.getByRole('textbox',{name:'Что делать иначе'}),'Сначала конкретный итог');
 await user.click(screen.getByRole('button',{name:'Сохранить уточнение'}));
 expect(api).toHaveBeenLastCalledWith('memory',expect.objectContaining({action:'refine',behavior:'Сначала конкретный итог',expected_version:2}));
 expect(await screen.findByText('Уточнение сохранено. Правило ожидает отдельного принятия.')).toBeTruthy();
});
it('показывает конфликт и позволяет обновить список',async()=>{
 const api=vi.fn().mockResolvedValueOnce({candidates:[rule],rules:[],history:[]}).mockRejectedValueOnce(new Error('Правило изменилось')).mockResolvedValue({candidates:[],rules:[],history:[]});
 const user=userEvent.setup();render(<Memory api={api}/>);
 await user.click(await screen.findByRole('button',{name:'Отклонить'}));
 expect((await screen.findByRole('alert')).textContent).toContain('Правило изменилось');
 await user.click(screen.getByRole('button',{name:'Обновить память'}));
 expect(await screen.findByText('Новых уроков для подтверждения пока нет.')).toBeTruthy();
});
it('обновляет решения команды при возвращении в приложение и сохраняет черновик',async()=>{
 const api=vi.fn().mockResolvedValue({candidates:[rule],rules:[],history:[]});
 const user=userEvent.setup();render(<Memory api={api}/>);
 await screen.findByText('Короткий ответ');
 fireEvent(window,new Event('focus'));
 await waitFor(()=>expect(api).toHaveBeenCalledTimes(2));
 await user.click(screen.getByRole('button',{name:'Уточнить'}));
 fireEvent(window,new Event('focus'));
 expect(api).toHaveBeenCalledTimes(2);
 expect(screen.getByRole('textbox',{name:'Что делать иначе'})).toBeTruthy();
});
