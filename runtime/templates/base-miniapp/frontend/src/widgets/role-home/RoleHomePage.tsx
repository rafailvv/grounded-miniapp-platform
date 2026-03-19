import { useNavigate } from 'react-router-dom';
import { ProfileCabinetCard } from '@/entities/profile/ui/ProfileCabinetCard/ProfileCabinetCard';
import type { AppRole } from '@/entities/role/model/role';
import { getClientProfileDisplayName, loadRoleProfileView } from '@/features/profile/model/profileStore';
import styles from '@/widgets/role-home/RoleHomePage.module.css';

type RoleHomePageProps = {
  role: AppRole;
  featureText: string;
};

export function RoleHomePage({ role, featureText }: RoleHomePageProps): JSX.Element {
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
