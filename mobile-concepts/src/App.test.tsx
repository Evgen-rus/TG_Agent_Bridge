import { describe, it, expect } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';

describe('Дизайн-сценарии',()=>{
 it('Rick: источник, отмена, подтверждение и защита от повтора',async()=>{
  const u=userEvent.setup();render(<App/>);
  await u.click(screen.getByRole('button',{name:'Разобрать предложение'}));
  await u.click(screen.getByRole('button',{name:'Посмотреть источник'}));
  expect(screen.getByText('«Подскажите, к какой дате можно ожидать план?»')).toBeTruthy();
  await u.click(screen.getByRole('button',{name:'Принять в демо'}));
  await u.click(screen.getByRole('button',{name:'Отмена'}));
  expect(screen.getByRole('button',{name:'Принять в демо'})).toBeTruthy();
  await u.click(screen.getByRole('button',{name:'Принять в демо'}));
  await u.click(screen.getByRole('button',{name:'Подтвердить в демо'}));
  expect((screen.getByRole('button',{name:'Принято'}) as HTMLButtonElement).disabled).toBe(true);
 });
 it('Sally: факт до подтверждения и изоляция диалогов по темам',async()=>{
  const u=userEvent.setup();render(<App/>);
  await u.click(screen.getByRole('button',{name:'Sally A Ledger'}));
  await u.click(screen.getByRole('button',{name:/Упаковочная линия Тема/}));
  await u.click(screen.getByRole('button',{name:'Проверить изменение факта'}));
  expect(screen.getAllByText('Желаемый срок — следующая неделя').length).toBe(2);
  await u.click(screen.getByRole('button',{name:'Подтвердить в демо'}));
  expect(screen.getByText('Желаемый срок — 23 октября')).toBeTruthy();
  await u.click(screen.getByRole('button',{name:'Обсудить с Салли'}));
  await u.type(screen.getByRole('textbox',{name:'Сообщение'}),'Уникальный вопрос темы 101');
  await u.click(screen.getByRole('button',{name:'Отправить в демо'}));
  await waitFor(()=>expect((screen.getByRole('textbox',{name:'Сообщение'}) as HTMLTextAreaElement).disabled).toBe(false));
  await u.click(within(screen.getByRole('navigation',{name:'Навигация приложения'})).getByRole('button',{name:'Сделки'}));
  await u.click(screen.getByRole('button',{name:/Оснащение мастерской/}));
  await u.click(screen.getByRole('button',{name:'Обсудить с Салли'}));
  expect(screen.queryByText('Уникальный вопрос темы 101')).toBeNull();
 });
 it('четыре варианта, поиск и восстановление после ошибки',async()=>{
  const u=userEvent.setup();render(<App/>);
  for(const name of ['Rick B Command','Sally B Momentum','Sally A Ledger','Rick A Signal']){
   await u.click(screen.getByRole('button',{name}));
   await u.click(within(screen.getByRole('navigation',{name:'Навигация приложения'})).getByRole('button',{name:name.startsWith('Rick')?'События':'Сделки'}));
   await u.type(screen.getByRole('textbox',{name:'Поиск'}),'нет такого');
   expect(screen.getByText('Ничего не найдено')).toBeTruthy();
   await u.click(screen.getByRole('button',{name:'Сбросить поиск'}));
   await u.selectOptions(screen.getByLabelText('СОСТОЯНИЯ ИНТЕРФЕЙСА'),'error');
   expect(screen.getByText('Не удалось загрузить данные')).toBeTruthy();
   await u.click(screen.getByRole('button',{name:'Повторить'}));
   expect(screen.getByRole('textbox',{name:'Поиск'})).toBeTruthy();
  }
 });
 it('Rick требует контекст и очищает переписку при смене концепции',async()=>{
  const u=userEvent.setup();render(<App/>);
  await u.click(within(screen.getByRole('navigation',{name:'Навигация приложения'})).getByRole('button',{name:'Диалог'}));
  expect((screen.getByRole('textbox',{name:'Сообщение'}) as HTMLTextAreaElement).disabled).toBe(true);
  await u.selectOptions(screen.getByLabelText('Контекст чата'),'Проект «Север»');
  await u.type(screen.getByRole('textbox',{name:'Сообщение'}),'Вопрос по Северу');
  await u.click(screen.getByRole('button',{name:'Отправить в демо'}));
  await u.click(screen.getByRole('button',{name:'Rick B Command'}));
  await u.click(within(screen.getByRole('navigation',{name:'Навигация приложения'})).getByRole('button',{name:'Диалог'}));
  expect(screen.queryByText('Вопрос по Северу')).toBeNull();
 });
});
