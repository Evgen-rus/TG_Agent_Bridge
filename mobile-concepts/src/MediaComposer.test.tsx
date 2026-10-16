import {it,expect,vi,afterEach} from 'vitest';
import {render,screen,waitFor,fireEvent,cleanup,act} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MediaComposer} from './MediaComposer';

afterEach(()=>{cleanup();vi.unstubAllGlobals()});

function microphone(){
 const stop=vi.fn();
 vi.stubGlobal('navigator',{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop}]})}});
 class Recorder{
  static isTypeSupported(){return true}
  state='inactive';mimeType='audio/webm';ondataavailable?: (e:{data:Blob})=>void;onstop?:()=>void;
  start(){this.state='recording'}
  stop(){this.state='inactive';this.ondataavailable?.({data:new Blob(['voice'])});this.onstop?.()}
 }
 vi.stubGlobal('MediaRecorder',Recorder);
 return stop;
}

it('Салли использует квадрат отмены и стрелку немедленной отправки',async()=>{
 const stop=microphone(),send=vi.fn(async()=>{});
 vi.stubGlobal('fetch',vi.fn(async()=>new Response(JSON.stringify({id:'voice-id'}),{status:201})));
 render(<MediaComposer visible product="sally" context="101" csrf="csrf" busy={false} onReplyClear={()=>{}} onError={()=>{}} onSend={send}/>);
 fireEvent.click(screen.getByRole('button',{name:'Записать голосовое'}));await act(async()=>{});
 expect(screen.getByRole('button',{name:'Отменить запись'}).querySelector('.lucide-square')).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'Отправить голосовое'}));
 await waitFor(()=>expect(send).toHaveBeenCalledWith('',['voice-id'],expect.any(String)));
 expect(stop).toHaveBeenCalledOnce();
});

it('ошибка немедленной отправки сохраняет голосовое для повтора',async()=>{
 microphone();const send=vi.fn(async()=>{}),errors:string[]=[];
 const fetch=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({error:'Нет сети'}),{status:503})).mockResolvedValueOnce(new Response(JSON.stringify({id:'voice-id'}),{status:201}));
 vi.stubGlobal('fetch',fetch);
 render(<MediaComposer visible product="rick" context="101" csrf="csrf" busy={false} onReplyClear={()=>{}} onError={s=>{if(s)errors.push(s)}} onSend={send}/>);
 fireEvent.click(screen.getByRole('button',{name:'Записать голосовое'}));
 await screen.findByRole('button',{name:'Отправить голосовое'});
 await waitFor(()=>expect(screen.getByRole('button',{name:'Отменить запись'})).toBeTruthy());
 await act(async()=>{});
 fireEvent.click(screen.getByRole('button',{name:'Отправить голосовое'}));
 await waitFor(()=>expect(errors[0]).toContain('Нет сети'));
 expect(screen.getByText('Голосовое.webm')).toBeTruthy();expect(send).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Отправить сообщение'}));
 await waitFor(()=>expect(send).toHaveBeenCalledWith('',['voice-id'],expect.any(String)));
});

it.each(['context','hidden','unmount'])('позднее разрешение микрофона не записывает после %s',async(reason)=>{
 const stop=vi.fn(),send=vi.fn(async()=>{});
 let resolve!:(value:MediaStream)=>void;
 vi.stubGlobal('navigator',{mediaDevices:{getUserMedia:()=>new Promise<MediaStream>(r=>{resolve=r})}});
 const props={visible:true,product:'rick',context:'101',csrf:'csrf',busy:false,onReplyClear:()=>{},onError:()=>{},onSend:send};
 const {rerender,unmount}=render(<MediaComposer {...props}/>);
 fireEvent.click(screen.getByRole('button',{name:'Записать голосовое'}));
 if(reason==='context')rerender(<MediaComposer {...props} context="202"/>);
 else if(reason==='hidden')rerender(<MediaComposer {...props} visible={false}/>);
 else unmount();
 await act(async()=>resolve({getTracks:()=>[{stop}]} as unknown as MediaStream));
 expect(stop).toHaveBeenCalledOnce();expect(send).not.toHaveBeenCalled();
});

it('переход из чата во время загрузки не отправляет голосовое даже после возврата',async()=>{
 microphone();const send=vi.fn(async()=>{});
 let resolve!:(value:Response)=>void;
 const fetch=vi.fn(()=>new Promise<Response>(r=>{resolve=r}));vi.stubGlobal('fetch',fetch);
 const props={visible:true,product:'rick',context:'101',csrf:'csrf',busy:false,onReplyClear:()=>{},onError:()=>{},onSend:send};
 const {rerender}=render(<MediaComposer {...props}/>);
 fireEvent.click(screen.getByRole('button',{name:'Записать голосовое'}));
 await act(async()=>{});
 fireEvent.click(screen.getByRole('button',{name:'Отправить голосовое'}));
 await waitFor(()=>expect(fetch).toHaveBeenCalledOnce());
 rerender(<MediaComposer {...props} visible={false}/>);rerender(<MediaComposer {...props}/>);
 await act(async()=>resolve(new Response(JSON.stringify({id:'voice-id'}),{status:201})));
 expect(send).not.toHaveBeenCalled();expect(screen.getByText('Голосовое.webm')).toBeTruthy();
});

it('отмена после нажатия стрелки блокирует отложенный onstop',async()=>{
 const stop=vi.fn(),send=vi.fn(async()=>{}),fetch=vi.fn();let finish!:()=>void;
 vi.stubGlobal('fetch',fetch);
 vi.stubGlobal('navigator',{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop}]})}});
 class Recorder{
  static isTypeSupported(){return true}
  state='inactive';mimeType='audio/webm';ondataavailable?: (e:{data:Blob})=>void;onstop?:()=>void;
  start(){this.state='recording'}
  stop(){this.state='inactive';finish=()=>{this.ondataavailable?.({data:new Blob(['voice'])});this.onstop?.()}}
 }
 vi.stubGlobal('MediaRecorder',Recorder);
 render(<MediaComposer visible product="rick" context="101" csrf="csrf" busy={false} onReplyClear={()=>{}} onError={()=>{}} onSend={send}/>);
 fireEvent.click(screen.getByRole('button',{name:'Записать голосовое'}));await act(async()=>{});
 fireEvent.click(screen.getByRole('button',{name:'Отправить голосовое'}));
 fireEvent.click(screen.getByRole('button',{name:'Отменить запись'}));
 act(()=>finish());
 expect(stop).toHaveBeenCalledOnce();expect(fetch).not.toHaveBeenCalled();expect(send).not.toHaveBeenCalled();expect(screen.queryByText('Голосовое.webm')).toBeNull();
});

it('сохраняет текст, файл и ключ после сетевого сбоя; повтор не загружает файл заново',async()=>{
 const u=userEvent.setup(),keys:string[]=[],errors:string[]=[];
 const send=vi.fn(async(_text:string,_ids:string[],key:string)=>{keys.push(key);if(keys.length===1)throw new Error('Нет сети')});
 const fetch=vi.fn(async()=>new Response(JSON.stringify({id:'private-file'}),{status:201}));vi.stubGlobal('fetch',fetch);
 const {container}=render(<MediaComposer visible product="rick" context="101" csrf="csrf" busy={false} onReplyClear={()=>{}} onError={s=>{if(s)errors.push(s)}} onSend={send}/>);
 await u.type(screen.getByLabelText('Сообщение'),'Вопрос по фото');
 await u.upload(container.querySelector('input[type=file]')!,new File(['image'],'screen.png',{type:'image/png'}));
 await u.click(screen.getByRole('button',{name:'Отправить сообщение'}));
 await waitFor(()=>expect(errors).toEqual(['Нет сети']));
 expect((screen.getByLabelText('Сообщение') as HTMLTextAreaElement).value).toBe('Вопрос по фото');
 expect(screen.getByText('screen.png')).toBeTruthy();
 await u.click(screen.getByRole('button',{name:'Отправить сообщение'}));
 await waitFor(()=>expect(keys.length).toBe(2));
 expect(keys[0]).toBe(keys[1]);expect(fetch).toHaveBeenCalledTimes(1);
 expect(send.mock.calls[1][1]).toEqual(['private-file']);
});

it('черновики разных контекстов восстанавливаются независимо',async()=>{
 const u=userEvent.setup();const props={visible:true,product:'sally',csrf:'csrf',busy:false,onReplyClear:()=>{},onError:()=>{},onSend:async()=>{}};
 const {rerender}=render(<MediaComposer {...props} context="101"/>);
 await u.type(screen.getByLabelText('Сообщение'),'Первая сделка');
 rerender(<MediaComposer {...props} context="0"/>);
 expect((screen.getByLabelText('Сообщение') as HTMLTextAreaElement).value).toBe('');
 await u.type(screen.getByLabelText('Сообщение'),'General');
 rerender(<MediaComposer {...props} context="101"/>);
 expect((screen.getByLabelText('Сообщение') as HTMLTextAreaElement).value).toBe('Первая сделка');
});

it('стрелка завершает запись и сразу отправляет аудиофайл',async()=>{
 const stop=vi.fn(),send=vi.fn(async()=>{}),u=userEvent.setup();
 vi.stubGlobal('navigator',{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop}]})}});
 class Recorder{
  static isTypeSupported(){return true}
  state='inactive';mimeType='audio/webm';ondataavailable?: (e:{data:Blob})=>void;onstop?:()=>void;
  start(){this.state='recording'}
  stop(){this.state='inactive';this.ondataavailable?.({data:new Blob(['voice'])});this.onstop?.()}
 }
 vi.stubGlobal('MediaRecorder',Recorder);
 vi.stubGlobal('URL',{createObjectURL:()=> 'blob:test',revokeObjectURL:()=>{}});
 vi.stubGlobal('fetch',vi.fn(async()=>new Response(JSON.stringify({id:'voice-id'}),{status:201})));
 render(<MediaComposer visible product="rick" context="101" csrf="csrf" busy={false} onReplyClear={()=>{}} onError={()=>{}} onSend={send}/>);
 fireEvent.click(screen.getByRole('button',{name:'Записать голосовое'}));
 const square=await screen.findByRole('button',{name:'Отменить запись'});
 expect(square.querySelector('.lucide-square')).toBeTruthy();expect(screen.getByRole('button',{name:'Отправить голосовое'}).querySelector('.lucide-arrow-up')).toBeTruthy();expect(square.querySelector('.lucide-x')).toBeNull();
 expect(screen.queryByRole('button',{name:'Записать голосовое'})).toBeNull();
 expect(screen.queryByRole('button',{name:'Отправить сообщение'})).toBeNull();
 await u.click(screen.getByRole('button',{name:'Отправить голосовое'}));
 expect(stop).toHaveBeenCalled();
 await waitFor(()=>expect(send).toHaveBeenCalledWith('',['voice-id'],expect.any(String)));
});


it('ошибка PDF сохраняет оба вложения и объясняет, какой файл заменить',async()=>{
 const u=userEvent.setup(),send=vi.fn(async()=>{}),errors:string[]=[];
 vi.stubGlobal('fetch',vi.fn(async()=>new Response(JSON.stringify({error:'PDF со сканами: максимум 10 страниц. Разделите PDF.'}),{status:422})));
 const {container}=render(<MediaComposer visible product="sally" context="0" csrf="csrf" busy={false} onReplyClear={()=>{}} onError={s=>{if(s)errors.push(s)}} onSend={send}/>);
 await u.upload(container.querySelector('input[type=file]')!,[new File(['pdf'],'contract.pdf',{type:'application/pdf'}),new File(['audio'],'voice.webm',{type:'audio/webm'})]);
 await u.click(screen.getByRole('button',{name:'Отправить сообщение'}));
 await waitFor(()=>expect(errors[0]).toContain('«contract.pdf»: PDF со сканами'));
 expect(errors[0]).toContain('Удалите этот файл крестиком');
 expect(screen.getByText('contract.pdf')).toBeTruthy();expect(screen.getByText('voice.webm')).toBeTruthy();
 expect(screen.queryByRole('status')).toBeNull();expect(send).not.toHaveBeenCalled();
 expect((screen.getByRole('button',{name:'Отправить сообщение'}) as HTMLButtonElement).disabled).toBe(false);
});


it('квадрат отменяет запись без отправки и без удаления старого текста',async()=>{
 const stop=vi.fn(),send=vi.fn(async()=>{});
 vi.stubGlobal('navigator',{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop}]})}});
 class Recorder{
  static isTypeSupported(){return true}
  state='inactive';mimeType='audio/webm';onstop?:()=>void;
  start(){this.state='recording'}
  stop(){this.state='inactive';this.onstop?.()}
 }
 vi.stubGlobal('MediaRecorder',Recorder);
 const u=userEvent.setup();
 render(<MediaComposer visible product="rick" context="0" csrf="csrf" busy={false} onReplyClear={()=>{}} onError={()=>{}} onSend={send}/>);
 await u.type(screen.getByLabelText('Сообщение'),'Мой вопрос');
 await u.click(screen.getByRole('button',{name:'Записать голосовое'}));
 await u.click(await screen.findByRole('button',{name:'Отменить запись'}));
 expect(stop).toHaveBeenCalled();expect(send).not.toHaveBeenCalled();
 expect(screen.queryByText('Голосовое.webm')).toBeNull();expect((screen.getByLabelText('Сообщение') as HTMLTextAreaElement).value).toBe('Мой вопрос');
 expect(screen.getByRole('button',{name:'Записать голосовое'})).toBeTruthy();
});
