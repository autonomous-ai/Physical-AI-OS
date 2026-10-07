import { EnvironmentCard } from "./sensing/EnvironmentCard";
import { VisionSensing } from "./sensing/VisionSensing";
import "./sensing/sensing.css";

export function SensingSection({ hasVision, hasEnvironment }: { hasVision: boolean; hasEnvironment: boolean }) {
  return (
    <div className="lm-sensing">
      {hasVision && <VisionSensing />}
      <EnvironmentCard available={hasEnvironment} />
    </div>
  );
}
