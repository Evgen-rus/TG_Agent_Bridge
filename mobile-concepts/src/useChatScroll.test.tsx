import {act,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {useChatScroll} from './useChatScroll';

afterEach(()=>vi.unstubAllGlobals());

function setup(){
 let height=1800,top=1200,resize=()=>{};
 const scroll=vi.fn((options?:ScrollToOptions|number,y?:number)=>{top=Math.max(0,(typeof options==='number'?y||0:Number(options?.top)||0)-600)});
 vi.stubGlobal('ResizeObserver',class{
  constructor(callback:()=>void){resize=callback}
  observe(){}disconnect(){}
 });
 vi.stubGlobal('matchMedia',()=>({matches:false}));
 function Chat({active=true,context='one',revision='1'}){
  const chat=useChatScroll(active,context,revision);
  return <><div ref={element=>{chat.screen.current=element;if(element){
   Object.defineProperties(element,{scrollHeight:{configurable:true,get:()=>height},clientHeight:{configurable:true,value:600},scrollTop:{configurable:true,get:()=>top}});
   element.scrollTo=scroll;
  }}} data-testid="screen" onScroll={chat.onScroll} onWheel={chat.interrupt}>
   <div ref={chat.messages}>Messages</div>
  </div>{chat.unread&&<button onClick={()=>chat.toBottom()}>New message</button>}</>;
 }
 return {Chat,scroll,resize:()=>resize(),grow:()=>{height+=500},readOld:()=>{fireEvent.wheel(screen.getByTestId('screen'));top=200;fireEvent.scroll(screen.getByTestId('screen'))},top:()=>top};
}

it('opens and reopens the end; follows messages and late image/viewport resizing',()=>{
 const s=setup();const {rerender}=render(<s.Chat/>);
 expect(s.scroll).toHaveBeenCalledWith({top:1800,behavior:'auto'});
 s.scroll.mockClear();s.grow();rerender(<s.Chat revision="2"/>);
 expect(s.scroll).toHaveBeenLastCalledWith({top:2300,behavior:'smooth'});
 s.grow();act(s.resize);expect(s.top()).toBe(2200);
 s.readOld();rerender(<s.Chat active={false}/>);rerender(<s.Chat/>);
 expect(s.top()).toBe(2200);
 s.readOld();rerender(<s.Chat context="two"/>);expect(s.top()).toBe(2200);
});

it('leaves history in place, ignores unchanged polling, and jumps on request',()=>{
 const s=setup();const {rerender}=render(<s.Chat/>);s.readOld();s.scroll.mockClear();
 rerender(<s.Chat/>);expect(screen.queryByText('New message')).toBeNull();
 s.grow();rerender(<s.Chat revision="2"/>);act(s.resize);
 expect(s.scroll).not.toHaveBeenCalled();expect(s.top()).toBe(200);
 fireEvent.click(screen.getByRole('button',{name:'New message'}));
 expect(s.scroll).toHaveBeenLastCalledWith({top:2300,behavior:'smooth'});
 expect(screen.queryByText('New message')).toBeNull();
 s.grow();rerender(<s.Chat revision="3"/>);expect(s.top()).toBe(2200);
});

it('honours reduced motion',()=>{
 const s=setup();vi.stubGlobal('matchMedia',()=>({matches:true}));
 const {rerender}=render(<s.Chat/>);s.readOld();s.grow();rerender(<s.Chat revision="2"/>);
 fireEvent.click(screen.getByRole('button',{name:'New message'}));
 expect(s.scroll).toHaveBeenLastCalledWith({top:2300,behavior:'auto'});
});


it('does not mistake intermediate smooth-scroll events for reading history',()=>{
 const s=setup();const {rerender}=render(<s.Chat/>);
 s.scroll.mockImplementation(()=>{});s.grow();rerender(<s.Chat revision="2"/>);
 fireEvent.scroll(screen.getByTestId('screen'));
 s.scroll.mockClear();s.grow();rerender(<s.Chat revision="3"/>);
 expect(s.scroll).toHaveBeenCalled();expect(screen.queryByText('New message')).toBeNull();
 s.readOld();s.scroll.mockClear();s.grow();rerender(<s.Chat revision="4"/>);
 expect(s.scroll).not.toHaveBeenCalled();expect(screen.getByText('New message')).toBeTruthy();
});
