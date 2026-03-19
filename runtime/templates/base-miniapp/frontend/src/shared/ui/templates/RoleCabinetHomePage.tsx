import { useNavigate } from 'react-router-dom';
import type { AppRole } from '@/shared/roles/role';
import { getClientProfileDisplayName, loadRoleProfileView } from '@/shared/profile/clientProfile';
import { ProfileCabinetCard } from '@/shared/ui/ProfileCabinetCard/ProfileCabinetCard';
import styles from '@/shared/ui/templates/RoleCabinetHomePage.module.css';

type RoleCabinetHomePageProps = {
  role: AppRole;
  featureText: string;
};

export function RoleCabinetHomePage({ role, featureText }: RoleCabinetHomePageProps): JSX.Element {
  const navigate = useNavigate();
  const profile = loadRoleProfileView(role);

  return (
    <section className={styles.page}>
      <ProfileCabinetCard
        displayName={getClientProfileDisplayName(profile)}
        roleLabel={profile.roleLabel}
        photoUrl={profile.photoUrl}
        onClick={() => navigate(`/${role}/profile`)}
      />

      <div className={styles.featureBlock}>
        <div className={styles.featureContent}>
          <span className={styles.featureTitle}>{profile.roleLabel} workspace</span>
          <span className={styles.featureText}>{featureText}</span>
        </div>
      </div>
    </section>
  );
}
