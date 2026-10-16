import {useEffect,useState} from 'react';

const supported=()=>typeof Notification!=='undefined'&&'serviceWorker'in navigator&&'PushManager'in window&&window.isSecureContext;
const applicationKey=(key:string)=>Uint8Array.from(atob(key.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-key.length%4)%4)),c=>c.charCodeAt(0));

export function PushSettings({csrf,product='rick'}:{csrf:string;product?:'rick'|'sally'}){
 const label=product==='rick'?'Рика':'Салли';
 const [enabled,setEnabled]=useState(false),[busy,setBusy]=useState(false),[message,setMessage]=useState(''),[denied,setDenied]=useState(()=>supported()&&Notification.permission==='denied');
 useEffect(()=>{let active=true;if(supported())Promise.all([navigator.serviceWorker.ready.then(r=>r.pushManager.getSubscription()),fetch(`/${product}/api/push-config`,{credentials:'same-origin',cache:'no-store'}).then(async r=>{if(!r.ok)throw new Error();return r.json()})]).then(([s,config])=>{if(active)setEnabled(!!s&&!!config.subscribed)}).catch(()=>{});return()=>{active=false}},[product]);
 const request=async(path:string,body?:unknown)=>{
  const response=await fetch(`/${product}/api/`+path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:body?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},...(body?{body:JSON.stringify(body)}:{})});
  const result=await response.json();if(!response.ok)throw new Error(result.error||'Не удалось настроить уведомления');return result;
 };
 const toggle=async()=>{
  setBusy(true);setMessage('');
  try{
   if(!enabled){
    // The permission prompt must remain directly inside this explicit user action.
    const permission=await Notification.requestPermission();setDenied(permission==='denied');
    if(permission!=='granted'){setMessage('Уведомления не включены. Разрешение можно изменить в настройках браузера.');return}
    const {public_key,subscribed}=await request('push-config');
    const registration=await navigator.serviceWorker.ready;
    let subscription=await registration.pushManager.getSubscription();
    if(subscription&&!subscribed){await subscription.unsubscribe();subscription=null}
    subscription=subscription||await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:applicationKey(public_key)});
    await request('push-subscribe',{subscription:subscription.toJSON()});setEnabled(true);setMessage('Уведомления включены на этом устройстве.');
   }else{
    await request('push-unsubscribe',{});
    const subscription=await(await navigator.serviceWorker.ready).pushManager.getSubscription();await subscription?.unsubscribe();
    setEnabled(false);setMessage('Уведомления отключены на этом устройстве.');
   }
  }catch(error){setMessage((error as Error).message)}finally{setBusy(false)}
 };
 return <article className="card"><h3>Уведомления {label}</h3><p>Напоминания приходят в Telegram и приложение. О готовых ответах {label} приложение сообщает отдельно. В уведомлении скрыты данные клиентов.</p>{!supported()?<p>Этот браузер не поддерживает уведомления. На iPhone установите приложение на главный экран и откройте оттуда.</p>:<><p>{denied?'Уведомления запрещены в настройках браузера.':enabled?'Включены на этом устройстве.':'Выключены на этом устройстве.'}</p><button className="button" disabled={busy||denied} onClick={toggle}>{busy?'Сохраняем…':enabled?'Отключить на этом устройстве':'Включить на этом устройстве'}</button></>}{message&&<p role="status">{message}</p>}</article>;
}
