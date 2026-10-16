import {useCallback,useEffect,useRef,useState} from 'react';
import {ArrowUp,Mic,Paperclip,Square,X} from 'lucide-react';

export type Attachment={id:string;filename:string;kind:string;mime?:string};
type Draft={text:string;files:{file:File;id?:string}[];key:string};
const blank=():Draft=>({text:'',files:[],key:crypto.randomUUID()});

export function MediaComposer({visible,product,context,csrf,busy,reply,onReplyClear,onSend,onError}:{visible:boolean;product:string;context:string;csrf:string;busy:boolean;reply?:number|null;onReplyClear:()=>void;onSend:(text:string,ids:string[],key:string)=>Promise<void>;onError:(s:string)=>void}){
 const drafts=useRef(new Map<string,Draft>());
 const [draft,setDraft]=useState<Draft>(blank),[working,setWorking]=useState(false),[recording,setRecording]=useState(false),[progress,setProgress]=useState('');
 const [preview,setPreview]=useState('');
 const recorder=useRef<MediaRecorder|null>(null),session=useRef<{cancelled:boolean;send:boolean}|null>(null),sending=useRef(false),mounted=useRef(true),fileInput=useRef<HTMLInputElement>(null);
 const active=useRef({context,product,visible});active.current={context,product,visible};
 const immediateVoice=product==='rick';
 const finishRecording=()=>{if(session.current&&recorder.current?.state==='recording'){session.current.send=true;recorder.current.stop()}};
 const cancelRecording=()=>{if(session.current)session.current.cancelled=true;if(recorder.current?.state==='recording')recorder.current.stop();setRecording(false)};
 useEffect(()=>{cancelRecording();const next=drafts.current.get(context)||blank();setDraft(next);setPreview('')},[context,product,visible]);
 useEffect(()=>()=>{if(preview)URL.revokeObjectURL(preview)},[preview]);
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;if(session.current)session.current.cancelled=true;if(recorder.current?.state==='recording')recorder.current.stop()}},[]);
 const change=(next:Draft)=>{drafts.current.set(context,next);setDraft(next)};
 const files=(selected:File[])=>{if(draft.files.length+selected.length>8){onError('Не более 8 вложений');return}if(selected.some(f=>f.size>20*1024*1024)){onError('Каждый файл должен быть до 20 МБ');return}change({...draft,files:[...draft.files,...selected.map(file=>({file}))],key:crypto.randomUUID()})};
 const record=async()=>{
  if(recording||working)return;
  const capture={cancelled:false,send:false};session.current=capture;setRecording(true);
  const valid=()=>mounted.current&&!capture.cancelled&&session.current===capture&&active.current.context===context&&active.current.product===product&&active.current.visible;
  let media:MediaStream|undefined;
  try{
   media=await navigator.mediaDevices.getUserMedia({audio:true});
   if(!valid()){media.getTracks().forEach(t=>t.stop());return}
   const mime=['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'].find(t=>MediaRecorder.isTypeSupported(t));
   const captureMedia=media,r=new MediaRecorder(media,mime?{mimeType:mime}:undefined);recorder.current=r;
   const chunks:BlobPart[]=[];let bytes=0;
   r.ondataavailable=e=>{bytes+=e.data.size;if(bytes>20*1024*1024){capture.cancelled=true;if(r.state==='recording')r.stop();if(mounted.current)onError('Запись превысила 20 МБ')}else chunks.push(e.data)};
   r.onstop=()=>{
    captureMedia.getTracks().forEach(t=>t.stop());
    if(recorder.current===r){recorder.current=null;if(mounted.current)setRecording(false)}
    if(!valid()||!capture.send)return;
    const blob=new Blob(chunks,{type:r.mimeType}),ext=r.mimeType.includes('mp4')?'m4a':r.mimeType.includes('ogg')?'ogg':'webm';
    const current=drafts.current.get(context)||blank();
    const next={...current,files:[...current.files,{file:new File([blob],`Голосовое.${ext}`,{type:r.mimeType})}],key:crypto.randomUUID()};
    if(next.files.length>8){onError('Не более 8 вложений');return}
    drafts.current.set(context,next);setDraft(next);if(immediateVoice)void submit(next,valid);else setPreview(URL.createObjectURL(blob));
   };
   r.start(1000);
  }catch{media?.getTracks().forEach(t=>t.stop());if(valid()){setRecording(false);onError('Не удалось включить микрофон. Проверьте разрешение браузера или прикрепите аудиофайл.')}}
 };
 const submit=async(message=draft,stillCurrent=()=>true)=>{
  if(sending.current||busy||(!message.text.trim()&&!message.files.length))return;
  sending.current=true;setWorking(true);onError('');
  const valid=()=>stillCurrent()&&mounted.current&&active.current.context===context&&active.current.product===product&&active.current.visible;
  try{
   for(const item of message.files){
    if(!valid())return;
    if(item.id)continue;
    setProgress(`Загружаем «${item.file.name}»…`);
    let response:Response;
    try{
     response=await fetch(`/${product}/api/upload?context=${encodeURIComponent(context)}&filename=${encodeURIComponent(item.file.name)}`,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':csrf},body:item.file,signal:AbortSignal.timeout(60000)});
     const result=await response.json().catch(()=>({error:response.status===413?'Файл больше 20 МБ. Уменьшите его или разделите на части.':'Сервер не принял файл. Повторите загрузку позже.'}));
     if(!response.ok)throw new Error(result.error||'Не удалось загрузить файл');item.id=result.id;
    }catch(e){const reason=(e as Error).name==='TimeoutError'?'Загрузка не завершилась за минуту. Проверьте соединение и повторите.':(e as Error).message;throw new Error(`«${item.file.name}»: ${reason} Сообщение с вложениями не отправлено. Удалите этот файл крестиком или замените его и повторите отправку.`)}
   }
   if(!valid())return;
   setProgress('Сохраняем сообщение…');await onSend(message.text,message.files.map(f=>f.id!),message.key+':'+(reply||''));
   const next=blank();drafts.current.set(context,next);
   if(valid()){setDraft(next);setPreview('');onReplyClear()}
  }catch(e){if(valid())onError((e as Error).message)}finally{sending.current=false;if(mounted.current)setWorking(false)}
 };
 return <div className="media-compose"><div className="attachment-draft">{reply&&<span>Ответ на #{reply}<button aria-label="Отменить ответ" onClick={onReplyClear}><X size={16}/></button></span>}{draft.files.map((f,i)=><span key={i}>{f.file.name}<button aria-label={`Удалить ${f.file.name}`} disabled={working} onClick={()=>change({...draft,files:draft.files.filter((_,n)=>n!==i),key:crypto.randomUUID()})}><X size={16}/></button></span>)}{preview&&<audio controls src={preview}/>}</div><form className="composer" onSubmit={e=>{e.preventDefault();if(!recording)void submit()}}><input ref={fileInput} type="file" multiple hidden accept="image/jpeg,image/png,image/webp,image/gif,audio/*,.txt,.md,.pdf,.docx" onChange={e=>{files(Array.from(e.target.files||[]));e.target.value=''}}/><button type="button" aria-label="Прикрепить файлы" disabled={working||recording} onClick={()=>fileInput.current?.click()}><Paperclip size={20}/></button><label className="sr-only" htmlFor="live-message">Сообщение</label><textarea id="live-message" rows={1} maxLength={6000} value={draft.text} disabled={working} onChange={e=>change({...draft,text:e.target.value,key:crypto.randomUUID()})} placeholder="Сообщение…"/>{recording?<button type="button" aria-label={immediateVoice?"Отменить запись":"Завершить запись"} title={immediateVoice?"Отменить запись":"Завершить запись"} onClick={immediateVoice?cancelRecording:finishRecording}><Square size={18}/></button>:<button type="button" aria-label="Записать голосовое" disabled={working} onClick={()=>void record()}><Mic size={20}/></button>}{recording?<button type="button" aria-label={immediateVoice?"Отправить голосовое":"Отменить запись"} title={immediateVoice?"Отправить голосовое":"Отменить запись"} disabled={immediateVoice&&busy} onClick={immediateVoice?finishRecording:cancelRecording}>{immediateVoice?<ArrowUp size={21}/>:<X size={18}/>}</button>:<button disabled={working||busy||(!draft.text.trim()&&!draft.files.length)} aria-label="Отправить сообщение"><ArrowUp size={21}/></button>}</form>{working&&<small role="status">{progress}</small>}</div>
}

export function MediaAttachments({items,product,context}:{items?:Attachment[];product:string;context:string}){
 return <div className="message-media">{items?.map(item=><MediaItem key={item.id} item={item} product={product} context={context}/>)}</div>
}
function MediaItem({item,product,context}:{item:Attachment;product:string;context:string}){
 const [url,setUrl]=useState(''),[error,setError]=useState(''),[loading,setLoading]=useState(false);
 useEffect(()=>()=>{if(url)URL.revokeObjectURL(url)},[url]);
 const load=useCallback(async()=>{setLoading(true);setError('');try{const response=await fetch(`/${product}/api/media?context=${encodeURIComponent(context)}&id=${encodeURIComponent(item.id)}`,{credentials:'same-origin',cache:'no-store'});if(!response.ok){const result=await response.json();throw new Error(result.error||'Вложение недоступно')}setUrl(URL.createObjectURL(await response.blob()));return true}catch(e){setError((e as Error).message);return false}finally{setLoading(false)}},[product,context,item.id]);
 useEffect(()=>{if(item.kind!=='image')return;let active=true;let timer:ReturnType<typeof setTimeout>|undefined;void load().then(ok=>{if(!ok&&active)timer=setTimeout(()=>void load(),3000)});return()=>{active=false;clearTimeout(timer)}},[load,item.kind]);
 return <div>{!url?<button className="text-button" disabled={loading} onClick={()=>void load()}>{loading?'Загрузка…':item.filename+' · открыть'}</button>:item.kind==='image'?<a href={url} target="_blank" rel="noreferrer"><img src={url} alt={item.filename}/></a>:item.kind==='audio'?<audio controls preload="metadata" src={url}/>:<a href={url} download={item.filename}>{item.filename} · скачать</a>}{error&&<small role="status">{error} <button className="text-button" onClick={()=>void load()}>Повторить</button></small>}</div>
}
