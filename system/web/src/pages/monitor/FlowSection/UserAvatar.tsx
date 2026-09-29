import { UserRound } from "lucide-react";
import { hwUrl } from "@/lib/api";

// Circular avatar: the enrolled face photo, else a generic icon (also on load error).
export function UserAvatar({ user, photo, size = 18, color }: {
  user: string;
  photo?: string;
  size?: number;
  color: string;
}) {
  const iconSize = Math.round(size * 0.62);
  return (
    <span
      aria-hidden
      style={{
        width: size, height: size, borderRadius: "50%", flexShrink: 0,
        overflow: "hidden", display: "inline-flex", alignItems: "center", justifyContent: "center",
        background: `${color}22`, color,
      }}
    >
      {photo ? (
        <img
          src={hwUrl(`/face/photo/${encodeURIComponent(user)}/${encodeURIComponent(photo)}`)}
          alt=""
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
          onError={(e) => {
            const img = e.currentTarget as HTMLImageElement;
            img.style.display = "none";
            const sib = img.nextElementSibling as HTMLElement | null;
            if (sib) sib.style.display = "inline-flex";
          }}
        />
      ) : null}
      <UserRound
        size={iconSize}
        strokeWidth={2.25}
        style={{ display: photo ? "none" : "inline-flex" }}
      />
    </span>
  );
}
