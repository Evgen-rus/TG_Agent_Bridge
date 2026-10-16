import {useState} from 'react';

export function ReminderApproval({canConfirm,onAction}:{canConfirm:boolean;onAction:(action:string,text?:string)=>Promise<void>}){
 const [refining,setRefining]=useState(false),[text,setText]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const act=async(action:string)=>{setBusy(true);setError('');try{await onAction(action,text);setRefining(false);setText('')}catch(e){setError((e as Error).message)}finally{setBusy(false)}};
 return <div className="reminder-approval">
  {error&&<p role="alert">{error}</p>}
  {refining?<form onSubmit={e=>{e.preventDefault();void act('refine')}}>
   <label>Уточнение напоминания<textarea aria-label="Уточнение напоминания" value={text} maxLength={6000} disabled={busy} onChange={e=>setText(e.target.value)}/></label>
   <button className="button" disabled={busy||!text.trim()}>Отправить уточнение</button>
  </form>:<>
   <button className="button" disabled={busy||!canConfirm} onClick={()=>void act('confirm')}>Подтвердить напоминание</button>
   <button className="text-button" disabled={busy} onClick={()=>setRefining(true)}>Уточнить</button>
  </>}
  <button className="text-button decline" disabled={busy} onClick={()=>void act('cancel')}>Отменить напоминание</button>
 </div>;
}
