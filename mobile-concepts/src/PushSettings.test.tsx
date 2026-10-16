import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {PushSettings} from './PushSettings';

afterEach(()=>{cleanup();vi.unstubAllGlobals()});

function browser(permission='default',subscribed=false,serverSubscribed=subscribed){
 const subscription={toJSON:()=>({endpoint:'https://fcm.googleapis.com/device',keys:{auth:'a',p256dh:'b'}}),unsubscribe:vi.fn(async()=>true)};
 const subscribe=vi.fn(async()=>subscription),requestPermission=vi.fn(async()=>permission==='denied'?'denied':'granted');
 vi.stubGlobal('isSecureContext',true);vi.stubGlobal('PushManager',class {});
 vi.stubGlobal('Notification',{permission,requestPermission});
 vi.stubGlobal('navigator',{serviceWorker:{ready:Promise.resolve({pushManager:{getSubscription:async()=>subscribed?subscription:null,subscribe}})}});
 const fetch=vi.fn(async(_url:string,options?:RequestInit)=>new Response(JSON.stringify(options?.method==='POST'?{subscribed:true}:{public_key:'BAAA',subscribed:serverSubscribed}),{status:200}));
 vi.stubGlobal('fetch',fetch);return {subscription,subscribe,requestPermission,fetch};
}

it('asks permission only on click and registers subscription with CSRF',async()=>{
 const {fetch,requestPermission,subscribe,subscription}=browser();
 render(<PushSettings csrf="session-csrf"/>);
 await waitFor(()=>expect(fetch).toHaveBeenCalledOnce());
 expect(requestPermission).not.toHaveBeenCalled();expect(subscribe).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Включить на этом устройстве'}));
 await screen.findByText('Уведомления включены на этом устройстве.');
 expect(requestPermission).toHaveBeenCalledOnce();
 expect(fetch).toHaveBeenLastCalledWith('/rick/api/push-subscribe',expect.objectContaining({method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':'session-csrf'},body:JSON.stringify({subscription:subscription.toJSON()})}));
});

it('revokes delivery on server before removing browser subscription',async()=>{
 const {fetch,subscription}=browser('granted',true);
 render(<PushSettings csrf="csrf"/>);
 fireEvent.click(await screen.findByRole('button',{name:'Отключить на этом устройстве'}));
 await screen.findByText('Уведомления отключены на этом устройстве.');
 expect(fetch).toHaveBeenLastCalledWith('/rick/api/push-unsubscribe',expect.objectContaining({method:'POST',body:'{}'}));
 expect(subscription.unsubscribe).toHaveBeenCalledOnce();
});

it('explains denied permissions without asking again',async()=>{
 const {requestPermission}=browser('denied');
 render(<PushSettings csrf="csrf"/>);
 expect(screen.getByText('Уведомления запрещены в настройках браузера.')).toBeTruthy();
 expect((screen.getByRole('button',{name:'Включить на этом устройстве'}) as HTMLButtonElement).disabled).toBe(true);
 expect(requestPermission).not.toHaveBeenCalled();
});

it('replaces a browser subscription from an expired or different login after explicit enable',async()=>{
 const {subscription,subscribe}=browser('granted',true,false);
 render(<PushSettings csrf="new-session"/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Включить на этом устройстве'})).toBeTruthy());
 fireEvent.click(screen.getByRole('button',{name:'Включить на этом устройстве'}));
 await screen.findByText('Уведомления включены на этом устройстве.');
 expect(subscription.unsubscribe).toHaveBeenCalledOnce();expect(subscribe).toHaveBeenCalledOnce();
});
