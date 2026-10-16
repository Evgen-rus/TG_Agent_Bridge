import {useCallback,useLayoutEffect,useRef,useState} from 'react';

export function useChatScroll(active:boolean,context:string,revision:string){
 const screen=useRef<HTMLDivElement>(null),messages=useRef<HTMLDivElement>(null);
 const following=useRef(true),automatic=useRef(false);
 const [unread,setUnread]=useState(false);
 const toBottom=useCallback((smooth=true)=>{
  const element=screen.current;if(!element)return;
  following.current=true;automatic.current=true;setUnread(false);
  element.scrollTo?.({top:element.scrollHeight,behavior:smooth&&!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches?'smooth':'auto'});
 },[]);
 const onScroll=()=>{
  if(!active||!screen.current)return;
  const element=screen.current,near=element.scrollHeight-element.clientHeight-element.scrollTop<80;
  if(automatic.current&&!near)return;
  automatic.current=false;following.current=near;
  if(near)setUnread(false);
 };
 const interrupt=()=>{automatic.current=false};
 useLayoutEffect(()=>{
  if(!active)return;
  toBottom(false);
  if(typeof ResizeObserver==='undefined'||!messages.current||!screen.current)return;
  const observer=new ResizeObserver(()=>{if(following.current)toBottom()});
  observer.observe(messages.current);observer.observe(screen.current);
  return()=>observer.disconnect();
 },[active,context,toBottom]);
 useLayoutEffect(()=>{
  if(!active)return;
  if(following.current)toBottom();else setUnread(true);
 },[active,context,revision,toBottom]);
 return {screen,messages,unread,toBottom,onScroll,interrupt};
}
