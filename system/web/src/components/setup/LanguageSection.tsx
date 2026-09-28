import { Globe } from "lucide-react";
import { SectionCard, LABEL_STYLE, INPUT_STYLE } from "./shared";

export function LanguageSection({
  active, sttLanguage, setSttLanguage,
}: {
  active: boolean;
  sttLanguage: string;
  setSttLanguage: (v: string) => void;
}) {
  return (
    <SectionCard id="language" title="Language" active={active} icon={<Globe size={17} />}
      description="Pick the language your robot listens for. You can change this anytime from the Edit page.">
      <div style={{ marginBottom: 4 }}>
        <label htmlFor="stt_language" style={LABEL_STYLE}>
          Language
        </label>
        <select
          id="stt_language"
          value={sttLanguage}
          onChange={(e) => setSttLanguage(e.target.value)}
          style={{ ...INPUT_STYLE, cursor: "pointer" }}
        >
          <option value="">Auto (default)</option>
          <option value="en">English</option>
          <option value="vi">Vietnamese</option>
          <option value="zh-CN">Chinese (Simplified)</option>
          <option value="zh-TW">Chinese (Traditional)</option>
        </select>
      </div>
    </SectionCard>
  );
}
