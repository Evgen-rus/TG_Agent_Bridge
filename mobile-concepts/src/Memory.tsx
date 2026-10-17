import {useEffect,useRef,useState} from 'react';

type Source={text?:string;url?:string;context?:string;author?:string;date?:string|number;source_id?:string};
type Rule={id:number;title:string;situation:string;behavior:string;rationale:string;scope:string;context:string;status:string;version:number;sources:Source[];updated_at?:string|number;previous_id?:number;author?:string;comment?:string};
type Snapshot={candidates:Rule[];rules:Rule[];history:Rule[]};
type Api=(path:string,body?:unknown)=>Promise<Snapshot>;
const labels:Record<string,string>={pending:'На подтверждении',refining:'На уточнении',active:'Действует',rejected:'Отклонено',superseded:'Заменено',disabled:'Отключено',revised:'Уточнено'};
const date=(value?:string|number)=>{if(!value)return '';const parsed=new Date(typeof value==='number'?value*1000:value);return Number.isNaN(parsed.getTime())?'':parsed.toLocaleString('ru-RU',{timeZone:'Asia/Novosibirsk'})};

export function Memory({api}:{api:Api}){
 const [data,setData]=useState<Snapshot|null>(null),[tab,setTab]=useState<keyof Snapshot>('candidates'),[error,setError]=useState(''),[busy,setBusy]=useState(false),[editing,setEditing]=useState<number|null>(null),[notice,setNotice]=useState('');
 const [draft,setDraft]=useState({title:'',situation:'',behavior:'',rationale:'',comment:''});
 const editingRef=useRef(editing);editingRef.current=editing;
 useEffect(()=>{let active=true;const load=()=>{if(editingRef.current!==null)return;api('memory').then(value=>{if(active){setData(value);setError('')}}).catch(e=>{if(active)setError(e.message)})};load();const timer=setInterval(load,15000);window.addEventListener('focus',load);return()=>{active=false;clearInterval(timer);window.removeEventListener('focus',load)}},[api]);
 const refresh=async()=>{setBusy(true);setError('');try{setData(await api('memory'))}catch(e){setError((e as Error).message)}finally{setBusy(false)}};
 const decide=async(rule:Rule,action:string)=>{setBusy(true);setError('');setNotice('');try{setData(await api('memory',{id:rule.id,action,expected_version:rule.version,...(action==='refine'?draft:{})}));setEditing(null);setNotice(action==='refine'?'Уточнение сохранено. Правило ожидает отдельного принятия.':'Решение сохранено.')}catch(e){setError((e as Error).message)}finally{setBusy(false)}};
 const edit=(rule:Rule)=>{setDraft({title:rule.title,situation:rule.situation,behavior:rule.behavior,rationale:rule.rationale,comment:''});setEditing(rule.id)};
 return <section className="memory" aria-label="Память агента"><h1 className="page-title">Память</h1><p className="subtitle">Подтверждённые уроки команды. Новое правило применяется после принятия.</p>
  <div className="memory-tabs" role="group" aria-label="Раздел памяти">{([['candidates','Кандидаты'],['rules','Действующие'],['history','История']] as const).map(([key,label])=><button className="button secondary" key={key} aria-pressed={tab===key} onClick={()=>{setTab(key);setEditing(null)}}>{label}{data?` (${data[key].length})`:''}</button>)}</div>
  {error&&<div className="live-error" role="alert"><p>{error}</p><button disabled={busy} onClick={()=>void refresh()}>Обновить память</button></div>}
  {notice&&<p role="status">{notice}</p>}
  {!data&&!error&&<p role="status">Загружаем память…</p>}
  {data&&!data[tab].length&&<p className="subtitle">{tab==='candidates'?'Новых уроков для подтверждения пока нет.':tab==='rules'?'Принятых правил пока нет. Они появятся после подтверждения командой.':'История решений пока пуста.'}</p>}
  {data?.[tab].map(rule=><article className="memory-rule" key={rule.id}><small>{labels[rule.status]||rule.status} · версия {rule.version}{date(rule.updated_at)&&` · ${date(rule.updated_at)}`}</small><h2>{rule.title}</h2><dl><dt>Когда применять</dt><dd>{rule.situation}</dd><dt>Что делать иначе</dt><dd>{rule.behavior}</dd><dt>Основание</dt><dd>{rule.rationale}</dd><dt>Область действия</dt><dd>{rule.scope==='global'?'Все диалоги агента':`${rule.scope}${rule.context?` · ${rule.context}`:''}`}</dd></dl>
   {(rule.author||rule.comment)&&<p className="fine full-text">{rule.author==='learning'?'Предложено по итогам обучения':rule.author&&`Автор: ${rule.author}`}{rule.comment&&` · ${rule.comment}`}</p>}
   {rule.previous_id&&<p className="fine">Редакция правила #{rule.previous_id}</p>}
   <details><summary>Источники ({rule.sources.length})</summary>{rule.sources.map((source,index)=><blockquote key={index}><p className="full-text">{source.text||'Текст источника не сохранён'}</p><cite>{[source.author,source.context,date(source.date)].filter(Boolean).join(' · ')}</cite>{source.url&&/^https?:\/\//i.test(source.url)&&<a href={source.url} target="_blank" rel="noreferrer">Открыть источник</a>}</blockquote>)}</details>
   {editing===rule.id?<form className="memory-edit" onSubmit={event=>{event.preventDefault();void decide(rule,'refine')}}>{([['title','Название'],['situation','Когда применять'],['behavior','Что делать иначе'],['rationale','Основание'],['comment','Комментарий к уточнению']] as const).map(([key,label])=><label key={key}>{label}<textarea value={draft[key]} maxLength={key==='comment'?4000:1500} required={key!=='comment'} onChange={event=>setDraft({...draft,[key]:event.target.value})}/></label>)}<p className="fine">Уточнение создаёт кандидата. Для применения потребуется принять его отдельно.</p><button className="button" disabled={busy}>Сохранить уточнение</button><button type="button" className="button secondary" disabled={busy} onClick={()=>setEditing(null)}>Отмена</button></form>:
   (tab==='candidates'||tab==='rules')&&<div className="memory-actions">{tab==='candidates'&&<button className="button" disabled={busy} onClick={()=>void decide(rule,'accept')}>Принять</button>}<button className="button secondary" disabled={busy} onClick={()=>edit(rule)}>Уточнить</button><button className="text-button decline" disabled={busy} onClick={()=>void decide(rule,tab==='rules'?'disable':'reject')}>{tab==='rules'?'Отключить':'Отклонить'}</button></div>}
  </article>)}
 </section>
}
