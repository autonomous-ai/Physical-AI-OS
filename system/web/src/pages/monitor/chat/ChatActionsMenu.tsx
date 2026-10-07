import { useCallback, useState } from "react";
import { DropdownMenu } from "radix-ui";
import { Download, MoreHorizontal, Trash2 } from "lucide-react";
import "./chat-actions-menu.css";

type Props = {
  canExport: boolean;
  canClear: boolean;
  onExport: () => void;
  onClear: () => void;
};

export function ChatActionsMenu({ canExport, canClear, onExport, onClear }: Props) {
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const anchor = useCallback((node: HTMLButtonElement | null) => {
    if (node) setContainer(node.closest<HTMLElement>(".lm-root") ?? node.ownerDocument.body);
  }, []);

  if (!canExport && !canClear) return null;

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button ref={anchor} type="button" className="lm-chat-actions-trigger" aria-label="Conversation actions" title="Conversation actions">
          <MoreHorizontal size={18} aria-hidden="true" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal container={container}>
        <DropdownMenu.Content className="lm-chat-actions-menu" align="end" sideOffset={6} collisionPadding={12}>
          {canExport && (
            <DropdownMenu.Item className="lm-chat-actions-item" onSelect={onExport}>
              <Download size={16} aria-hidden="true" />
              Export conversation
            </DropdownMenu.Item>
          )}
          {canClear && (
            <DropdownMenu.Item className="lm-chat-actions-item lm-chat-actions-item-danger" onSelect={onClear}>
              <Trash2 size={16} aria-hidden="true" />
              Clear local chat history
            </DropdownMenu.Item>
          )}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
