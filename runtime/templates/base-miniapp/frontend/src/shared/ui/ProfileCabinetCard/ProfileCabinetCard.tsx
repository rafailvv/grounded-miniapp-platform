import styles from '@/shared/ui/ProfileCabinetCard/ProfileCabinetCard.module.css';

type ProfileCabinetCardProps = {
  displayName: string;
  roleLabel: string;
  photoUrl: string | null;
  onClick: () => void;
};

function getInitials(name: string): string {
  const parts = name
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2);

  if (parts.length === 0) return '🙂';
  return parts.map((part) => part[0]?.toUpperCase() ?? '').join('');
}

export function ProfileCabinetCard({ displayName, roleLabel, photoUrl, onClick }: ProfileCabinetCardProps): JSX.Element {
  return (
    <button className={styles.card} type="button" onClick={onClick}>
      <div className={styles.avatarWrap}>
        {photoUrl ? (
          <img className={styles.avatar} src={photoUrl} alt={displayName} />
        ) : (
          <div className={styles.avatarFallback} aria-hidden="true">
            {getInitials(displayName)}
          </div>
        )}
      </div>

      <div className={styles.info}>
        <span className={styles.caption}>Личный кабинет</span>
        <strong className={styles.name}>{displayName}</strong>
        {roleLabel ? <span className={styles.role}>{roleLabel}</span> : null}
      </div>

      <span className={styles.chevron} aria-hidden="true">
        ›
      </span>
    </button>
  );
}
