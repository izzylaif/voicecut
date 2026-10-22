import { ArrowDownToLine, Loader2 } from 'lucide-react';
import { installUpdate, updateButton, useUpdate } from '../updates';

/** The desktop app's download button, shown while a newer version is out. */
export function UpdateButton() {
  const button = updateButton(useUpdate());
  if (!button) return null;
  return (
    <button
      type="button"
      className="icon-button update-button"
      title={button.title}
      aria-label={button.title}
      aria-busy={button.busy}
      onClick={installUpdate}
    >
      {button.busy ? <Loader2 size={16} className="spin" /> : <ArrowDownToLine size={16} />}
    </button>
  );
}
