import { useState } from "react";
import { Users, Mic, ScanFace, UserCheck, UserPlus, RefreshCw } from "lucide-react";
import { S } from "./styles";
import { useTheme } from "@/lib/useTheme";
import { hwUrl } from "@/lib/api";
import { UserTimelineModal } from "./UserTimelineModal";
import { useStrangers } from "./face-owners/useStrangers";
import { useFilePreview } from "./face-owners/useFilePreview";
import { useFaceData } from "./face-owners/useFaceData";
import { useOwnerActions } from "./face-owners/useOwnerActions";
import { HeroStat } from "./face-owners/HeroStat";
import { EmptyState } from "./face-owners/EmptyState";
import { ConfirmDialog } from "./face-owners/ConfirmDialog";
import { RenameModal } from "./face-owners/RenameModal";
import { EnrollModal } from "./face-owners/EnrollModal";
import { UnknownFacesCard } from "./face-owners/UnknownFacesCard";
import { CooldownsCard } from "./face-owners/CooldownsCard";
import { StrangerClustersCard } from "./face-owners/StrangerClustersCard";
import { PersonCard } from "./face-owners/PersonCard";

export function FaceOwnersSection() {
  const [, , themeClass] = useTheme();

  // Enrolled-owners list + detection state (cooldowns, current user) + polling
  // and refresh live in their own hook. `refresh` reloads the list after a
  // mutation (enroll / rename / remove).
  const {
    data, error, currentUser,
    cooldowns, cdError, resetting, manualRefreshing,
    refresh, handleManualRefresh, handleResetCooldowns,
  } = useFaceData();

  // Owner-mutation flows (enroll / rename / remove user-photo-voice) + their
  // confirm/in-flight state live in their own hook; it takes `refresh` to reload
  // the list after a change.
  const {
    showEnroll, setShowEnroll,
    enrollName, setEnrollName,
    enrollTgUsername, setEnrollTgUsername,
    enrollTgId, setEnrollTgId,
    enrollFile, setEnrollFile,
    enrollPreview, enrolling, enrollError,
    enrollDragging, setEnrollDragging,
    fileInputRef,
    handleEnroll,
    renaming, setRenaming,
    renameValue, setRenameValue,
    renameError, setRenameError,
    renameSaving,
    handleRename, submitRename,
    confirmDelete, setConfirmDelete,
    confirmPhoto, setConfirmPhoto,
    confirmVoice, setConfirmVoice,
    deleting, deletingPhoto,
    handleRemove, confirmRemove,
    handleRemovePhoto, confirmRemovePhoto,
    handleRemoveVoiceFile, confirmRemoveVoice,
  } = useOwnerActions(refresh);

  // Timeline modal state
  const [timelineUser, setTimelineUser] = useState<string | null>(null);

  // Person card expand state — cards start collapsed so the grid stays dense.
  // Auto-expands the currently-active user the first time it appears.
  const [expandedPerson, setExpandedPerson] = useState<Record<string, boolean>>({});
  // Tracks which card is hovered so its action buttons fade in (cleaner UX
  // than a permanent row of icons cluttering every card).
  const [hoveredPerson, setHoveredPerson] = useState<string | null>(null);
  // Tracks the hovered photo thumbnail so only its delete button shows —
  // identified by "label/filename".
  const [hoveredPhoto, setHoveredPhoto] = useState<string | null>(null);

  // Unknown voice clusters + face stranger visit stats live in their own hook
  // (independent of the enrolled-owners data).
  const {
    strangers, strangersError,
    expandedCluster, setExpandedCluster,
    deletingCluster, deletingStrangerFile,
    faceStrangers, faceStrangersError,
    confirmCluster, setConfirmCluster,
    confirmStrangerFile, setConfirmStrangerFile,
    handleDeleteCluster, confirmDeleteCluster,
    handleDeleteStrangerFile, confirmDeleteStrangerFile,
  } = useStrangers();

  // Per-person file gallery: folder toggle, inline preview, audio playback, and
  // file-open routing live in their own hook.
  const {
    expanded, toggleDir,
    preview, setPreview, previewLoading,
    playingAudio,
    openFile,
  } = useFilePreview();


  // Base card style matching Overview/System: the `.lm-mon-card` class owns the
  // resting + hover box-shadow (and the gradient sheen / amber accent / glow), so
  // we strip the inline boxShadow from S.card to let the class's :hover win.
  const monCard = { ...S.card, boxShadow: undefined };

  // Sizing-only — visual surface/border/hover/focus comes from `.lm-u-input`.
  const inputStyle: React.CSSProperties = {
    fontSize: 12,
    padding: "8px 11px",
    borderRadius: 7,
    width: "100%",
  };

  // Small uppercase field label for the enroll form, so each input reads as a
  // labelled field rather than a bare placeholder box.
  const fieldLabel: React.CSSProperties = {
    display: "block", fontSize: 10, fontWeight: 700, letterSpacing: "0.06em",
    textTransform: "uppercase", color: "var(--lm-text-dim)", marginBottom: 5,
  };

  // Sizing-only — visual surface/border/hover/focus comes from `.lm-u-btn`.
  const btnStyle: React.CSSProperties = {
    fontSize: 10,
    padding: "4px 12px",
    borderRadius: 6,
    fontWeight: 600,
  };

  // Card header row — label on the left, badge/action on the right, matching the
  // Overview/System header pattern (no tinted strip, just spacing + alignment).
  const cardHeader: React.CSSProperties = {
    display: "flex", justifyContent: "space-between", alignItems: "center",
    marginBottom: 12,
  };

  // Square icon button — used for the per-person action row (Edit / Timeline /
  // Delete / Expand) so each is the same compact size regardless of label width.
  // Surface/border/hover come from `.lm-u-btn`.
  const iconBtnStyle: React.CSSProperties = {
    width: 26, height: 26,
    display: "inline-flex", alignItems: "center", justifyContent: "center",
    padding: 0, borderRadius: 5,
    color: "var(--lm-text-dim)",
    fontSize: 13,
    lineHeight: 1,
  };

  const allCooldownEntries = [
    ...(cooldowns?.owners ?? []),
    ...(cooldowns?.strangers ?? []),
  ];
  const hasActiveCooldowns = allCooldownEntries.some((e) => e.cooldown_remaining > 0);

  // "Here now" only names a concrete enrolled user; the "unknown" bucket means
  // someone is present but unrecognized, which reads better as a dash on the tile.
  const hereNow = currentUser && currentUser !== "unknown" ? currentUser : null;
  // First enrolled photo of the active user, so the Here-now tile can show a real
  // face avatar instead of the generic icon when we have one.
  const hereNowPhoto = hereNow
    ? data?.persons.find((p) => p.label === hereNow)?.photos?.[0] ?? null
    : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* Hero — command-center header for the Users page: title + live stat tiles,
          mirroring the Overview hero so the tab reads as a dashboard, not a list. */}
      <div className="lm-mon-hero">
        <div style={{ position: "relative", zIndex: 1, display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
              <div style={{
                width: 44, height: 44, borderRadius: 12, flexShrink: 0,
                display: "flex", alignItems: "center", justifyContent: "center",
                background: "var(--lm-amber-dim)", color: "var(--lm-amber)",
                boxShadow: "inset 0 0 0 1px var(--lm-amber-glow)",
              }} aria-hidden><Users size={22} /></div>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 19, fontWeight: 700, color: "var(--lm-text)", letterSpacing: "-0.3px", lineHeight: 1.2 }}>
                  Users
                </div>
                <div style={{ fontSize: 12, color: "var(--lm-text-dim)", marginTop: 2 }}>
                  {error
                    ? <span style={{ color: "var(--lm-red)" }}>User recognizer unavailable</span>
                    : "Enrolled people, unknown voices & faces seen by the robot"}
                </div>
              </div>
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <button
                onClick={() => setShowEnroll(!showEnroll)}
                className={"lm-u-btn" + (showEnroll ? "" : " lm-u-btn-primary")}
                style={{
                  ...btnStyle, fontSize: 12, padding: "7px 14px",
                  display: "inline-flex", alignItems: "center", gap: 5,
                  ...(showEnroll ? { background: "var(--lm-amber-dim)", color: "var(--lm-amber)", borderColor: "var(--lm-amber)" } : {}),
                }}
              >
                <UserPlus size={13} /> Enroll
              </button>
              <button
                onClick={handleManualRefresh}
                disabled={manualRefreshing}
                className="lm-u-btn"
                title="Refresh"
                aria-label="Refresh"
                style={{ ...btnStyle, fontSize: 12, padding: "7px 11px", color: "var(--lm-text-dim)", display: "inline-flex", alignItems: "center" }}
              >
                <RefreshCw size={13} className={manualRefreshing ? "lm-spin" : undefined} />
              </button>
            </div>
          </div>

          {/* Live stat tiles — headline numbers pulled up from the cards below. */}
          <div className="lm-grid-auto">
            <HeroStat icon={<Users size={16} />} label="Enrolled" tone="amber"
              value={data ? data.enrolled_count : "—"} />
            <HeroStat
              icon={hereNowPhoto && hereNow ? (
                <img
                  src={hwUrl(`/face/photo/${encodeURIComponent(hereNow)}/${encodeURIComponent(hereNowPhoto)}`)}
                  alt=""
                  style={{ width: "100%", height: "100%", objectFit: "cover", borderRadius: "inherit" }}
                  onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                />
              ) : <UserCheck size={16} />}
              label="Here now" tone="teal" pulse={!!hereNow}
              value={<span style={{ textTransform: "capitalize" }}>{hereNow ?? "—"}</span>} />
            <HeroStat icon={<Mic size={16} />} label="Unknown voices" tone="purple"
              value={strangers ? strangers.total : "—"} />
            <HeroStat icon={<ScanFace size={16} />} label="Unknown faces" tone="red"
              value={faceStrangers ? faceStrangers.length : "—"} />
          </div>
        </div>
      </div>

      {/* Enroll form — Add New User popup modal (keeps the dense person grid
          uncluttered). All enroll state + handleEnroll stay in this component. */}
      {showEnroll && (
        <EnrollModal
          themeClass={themeClass}
          enrollName={enrollName} setEnrollName={setEnrollName}
          enrollTgUsername={enrollTgUsername} setEnrollTgUsername={setEnrollTgUsername}
          enrollTgId={enrollTgId} setEnrollTgId={setEnrollTgId}
          enrollFile={enrollFile} setEnrollFile={setEnrollFile}
          enrollPreview={enrollPreview}
          enrolling={enrolling} enrollError={enrollError}
          enrollDragging={enrollDragging} setEnrollDragging={setEnrollDragging}
          fileInputRef={fileInputRef}
          onClose={() => setShowEnroll(false)}
          onSubmit={handleEnroll}
          inputStyle={inputStyle} fieldLabel={fieldLabel} btnStyle={btnStyle}
        />
      )}

      {/* Rename modal — themed replacement for the native prompt()/alert(). */}
      {renaming != null && (
        <RenameModal
          themeClass={themeClass}
          renameValue={renameValue} setRenameValue={setRenameValue}
          renameError={renameError} setRenameError={setRenameError}
          renameSaving={renameSaving}
          onClose={() => setRenaming(null)}
          onSubmit={submitRename}
          inputStyle={inputStyle} fieldLabel={fieldLabel} btnStyle={btnStyle}
        />
      )}

      {/* Delete-user confirm — themed replacement for window.confirm(). */}
      {confirmDelete != null && (
        <ConfirmDialog
          danger
          title={`Remove "${confirmDelete}"?`}
          message="All enrolled photos for this user will be permanently deleted."
          confirmLabel="Remove"
          onConfirm={confirmRemove}
          onCancel={() => setConfirmDelete(null)}
        />
      )}

      {/* Delete-photo confirm — single face photo from a user. */}
      {confirmPhoto != null && (
        <ConfirmDialog
          danger
          title="Delete this photo?"
          message={<>Remove <code style={{ color: "var(--lm-text)" }}>{confirmPhoto.filename}</code> from <span style={{ color: "var(--lm-text)", textTransform: "capitalize" }}>{confirmPhoto.label}</span>.</>}
          confirmLabel="Delete"
          onConfirm={confirmRemovePhoto}
          onCancel={() => setConfirmPhoto(null)}
        />
      )}

      {/* Delete-voice-sample confirm. */}
      {confirmVoice != null && (
        <ConfirmDialog
          danger
          title="Delete this voice sample?"
          message={<>Remove <code style={{ color: "var(--lm-text)" }}>{confirmVoice.filename}</code> from <span style={{ color: "var(--lm-text)", textTransform: "capitalize" }}>{confirmVoice.label}</span>.</>}
          confirmLabel="Delete"
          onConfirm={confirmRemoveVoice}
          onCancel={() => setConfirmVoice(null)}
        />
      )}

      {/* Delete stranger voice cluster confirm. */}
      {confirmCluster != null && (
        <ConfirmDialog
          danger
          title="Delete this cluster?"
          message={<>Cluster <code style={{ color: "var(--lm-text)" }}>{confirmCluster.hash}</code> ({confirmCluster.sampleCount} sample{confirmCluster.sampleCount !== 1 ? "s" : ""}) and its centroid will be removed.</>}
          confirmLabel="Delete"
          onConfirm={confirmDeleteCluster}
          onCancel={() => setConfirmCluster(null)}
        />
      )}

      {/* Delete stranger sample file confirm. */}
      {confirmStrangerFile != null && (
        <ConfirmDialog
          danger
          title="Delete this sample?"
          message={<>Remove <code style={{ color: "var(--lm-text)" }}>{confirmStrangerFile.filename}</code> from <code style={{ color: "var(--lm-text)" }}>{confirmStrangerFile.hash}</code>.</>}
          confirmLabel="Delete"
          onConfirm={confirmDeleteStrangerFile}
          onCancel={() => setConfirmStrangerFile(null)}
        />
      )}

      {/* Person cards */}
      {data && data.persons.length > 0 && (
        <div className="lm-grid-4">
          {data.persons.map((person, idx) => (
            <PersonCard
              key={person.label}
              person={person}
              idx={idx}
              currentUser={currentUser}
              expandedPerson={expandedPerson}
              setExpandedPerson={setExpandedPerson}
              hoveredPerson={hoveredPerson}
              setHoveredPerson={setHoveredPerson}
              hoveredPhoto={hoveredPhoto}
              setHoveredPhoto={setHoveredPhoto}
              expanded={expanded}
              toggleDir={toggleDir}
              deleting={deleting}
              deletingPhoto={deletingPhoto}
              preview={preview}
              previewLoading={previewLoading}
              setPreview={setPreview}
              playingAudio={playingAudio}
              onRename={handleRename}
              onRemove={handleRemove}
              onRemovePhoto={handleRemovePhoto}
              onRemoveVoiceFile={handleRemoveVoiceFile}
              onOpenFile={openFile}
              onTimeline={setTimelineUser}
              monCard={monCard}
              iconBtnStyle={iconBtnStyle}
            />
          ))}
        </div>
      )}

      {data && data.persons.length === 0 && !showEnroll && (
        <div className="lm-mon-card" style={monCard}>
          <EmptyState icon={<UserPlus size={20} />} text={`No users enrolled yet. Click "Enroll" above or send a photo via Telegram.`} />
        </div>
      )}

      {/* Bottom row: 3 diagnostic cards side-by-side so we get the same
          horizontal density as Sensing/Analytics, instead of three full-width
          stacks. */}
      <div className="lm-grid-3">

      {/* Unknown Voice Clusters */}
      <StrangerClustersCard
        strangers={strangers}
        strangersError={strangersError}
        expandedCluster={expandedCluster}
        setExpandedCluster={setExpandedCluster}
        deletingCluster={deletingCluster}
        deletingStrangerFile={deletingStrangerFile}
        onDeleteCluster={handleDeleteCluster}
        onDeleteStrangerFile={handleDeleteStrangerFile}
        monCard={monCard}
        cardHeader={cardHeader}
      />

      {/* Unknown Faces (visit stats per stranger_id) */}
      <UnknownFacesCard
        faceStrangers={faceStrangers}
        faceStrangersError={faceStrangersError}
        monCard={monCard}
        cardHeader={cardHeader}
      />

      {/* Face Recognition Cooldowns */}
      <CooldownsCard
        allCooldownEntries={allCooldownEntries}
        cdError={cdError}
        hasActiveCooldowns={hasActiveCooldowns}
        resetting={resetting}
        onReset={handleResetCooldowns}
        monCard={monCard}
        cardHeader={cardHeader}
      />

      </div>{/* /lm-grid-3 bottom row */}

      {timelineUser && (
        <UserTimelineModal user={timelineUser} onClose={() => setTimelineUser(null)} />
      )}
    </div>
  );
}
