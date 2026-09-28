import { Link2 } from "lucide-react";
import { BuddyCard } from "./BuddyCard";
import { HarnessCard } from "./HarnessCard";

export function PairingSection() {
  return (
    <section className="lm-pairing">
      <div className="lm-pairing-hero">
        <span className="lm-pairing-hero-icon" aria-hidden><Link2 size={20} /></span>
        <div>
          <div className="lm-pairing-eyebrow">ROBOT CONNECTIONS</div>
          <h1 className="lm-pairing-title">Pair a computer</h1>
          <p className="lm-pairing-description">
            Connect a Mac with Autonomous Buddy, or pair Harness to access this robot's agents.
          </p>
        </div>
      </div>
      <div className="lm-pairing-grid">
        <BuddyCard />
        <HarnessCard />
      </div>
    </section>
  );
}
