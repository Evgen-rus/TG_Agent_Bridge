import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {ReminderApproval} from './ReminderApproval';

afterEach(cleanup);
it('requires a valid date before confirming and submits the clarification',async()=>{
 const action=vi.fn().mockResolvedValue(undefined);
 render(<ReminderApproval canConfirm={false} onAction={action}/>);
 expect((screen.getByRole('button',{name:'Подтвердить напоминание'}) as HTMLButtonElement).disabled).toBe(true);
 fireEvent.click(screen.getByRole('button',{name:'Уточнить'}));
 fireEvent.change(screen.getByRole('textbox',{name:'Уточнение напоминания'}),{target:{value:'Завтра в 11'}});
 fireEvent.click(screen.getByRole('button',{name:'Отправить уточнение'}));
 await waitFor(()=>expect(action).toHaveBeenCalledWith('refine','Завтра в 11'));
});
it('preserves the card and shows failure when confirmation was rejected',async()=>{
 const action=vi.fn().mockRejectedValue(new Error('Обновите карточку'));
 render(<ReminderApproval canConfirm onAction={action}/>);
 fireEvent.click(screen.getByRole('button',{name:'Подтвердить напоминание'}));
 expect(await screen.findByRole('alert')).toHaveProperty('textContent','Обновите карточку');
 expect((screen.getByRole('button',{name:'Подтвердить напоминание'}) as HTMLButtonElement).disabled).toBe(false);
});
