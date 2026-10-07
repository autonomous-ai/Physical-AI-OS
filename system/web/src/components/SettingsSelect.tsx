import { Children, isValidElement, useCallback, useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import { Select } from "radix-ui";
import { Check, ChevronDown, ChevronUp } from "lucide-react";
import "./settings-select.css";

type OptionProps = { value: string | number; children: ReactNode; disabled?: boolean };
type Props = Omit<ComponentProps<"button">, "value" | "onChange" | "children"> & {
  value: string | number;
  onValueChange: (value: string) => void;
  children: ReactNode;
};

// Keep option declarations next to each form's provider-specific logic. Prefix
// values so an empty-string option (Auto/default) remains selectable in Radix.
export function SettingsSelect({ value, onValueChange, children, style, className, disabled, name, ...props }: Props) {
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const anchor = useCallback((node: HTMLButtonElement | null) => {
    if (node) setContainer(node.closest<HTMLElement>(".lm-root") ?? node.ownerDocument.body);
  }, []);
  const options = Children.toArray(children).filter(isValidElement<OptionProps>);
  return (
    <Select.Root value={`option:${value}`} onValueChange={(next) => onValueChange(next.slice(7))} disabled={disabled} name={name}>
      <Select.Trigger {...props} ref={anchor} type="button" className={`lm-settings-select ${className ?? ""}`} style={style}>
        <Select.Value />
        <Select.Icon asChild><ChevronDown size={16} aria-hidden="true" /></Select.Icon>
      </Select.Trigger>
      <Select.Portal container={container}>
        <Select.Content className="lm-settings-select-menu" position="popper" align="start" sideOffset={6} collisionPadding={12}>
          <Select.ScrollUpButton className="lm-settings-select-scroll"><ChevronUp size={16} /></Select.ScrollUpButton>
          <Select.Viewport className="lm-settings-select-options">
            {options.map((option, index) => (
              <Select.Item key={`${option.props.value}:${index}`} value={`option:${option.props.value}`} disabled={option.props.disabled} className="lm-settings-select-option">
                <Select.ItemText>{option.props.children}</Select.ItemText>
                <Select.ItemIndicator className="lm-settings-select-check"><Check size={16} aria-hidden="true" /></Select.ItemIndicator>
              </Select.Item>
            ))}
          </Select.Viewport>
          <Select.ScrollDownButton className="lm-settings-select-scroll"><ChevronDown size={16} /></Select.ScrollDownButton>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}
