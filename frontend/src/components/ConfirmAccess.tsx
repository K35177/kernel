import { useEffect, useRef } from 'react';

interface Props { busy: boolean; onConfirm(): void; onCancel(): void }
export function ConfirmAccess({ busy, onConfirm, onCancel }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) {
      if (typeof element.showModal === 'function') element.showModal();
      else element.setAttribute('open', '');
    }
    return () => { if (element?.open) element.close?.(); };
  }, []);
  return <dialog ref={dialog} aria-labelledby="confirm-title"
    onCancel={(event) => { event.preventDefault(); if (!busy) onCancel(); }}
    onClick={(event) => { if (event.target === event.currentTarget && !busy) onCancel(); }}>
    <div className="confirmation">
      <p className="eyebrow">Root access</p>
      <h2 id="confirm-title">Отправить заявку?</h2>
      <p>kernel передаст владельцу ваше имя, username и Telegram ID. Решение о доступе принимает владелец.</p>
      <p>После одобрения потребуется отдельно вступить в группу через Telegram.</p>
      <div className="actions">
        <button type="button" autoFocus disabled={busy} onClick={onCancel}>Отмена</button>
        <button type="button" disabled={busy} onClick={onConfirm}>{busy ? 'Отправка…' : 'Подтвердить'}</button>
      </div>
    </div>
  </dialog>;
}
