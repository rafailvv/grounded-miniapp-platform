import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { AppRole } from '@/shared/roles/role';
import { getClientProfileDisplayName, loadRoleProfileView } from '@/shared/profile/clientProfile';
import { fetchRoleDashboard, type RemoteDashboard } from '@/shared/profile/profileApi';
import { ProfileCabinetCard } from '@/shared/ui/ProfileCabinetCard/ProfileCabinetCard';
import generatedRoleExperience from '@/shared/generated/role-experience.json';
import styles from '@/shared/ui/templates/RoleCabinetHomePage.module.css';

type RoleCabinetHomePageProps = {
  role: AppRole;
  featureText: string;
};

export function RoleCabinetHomePage({ role, featureText }: RoleCabinetHomePageProps): JSX.Element {
  const navigate = useNavigate();
  const profile = loadRoleProfileView(role);
  const [dashboard, setDashboard] = useState<RemoteDashboard | null>(null);

  useEffect(() => {
    let isMounted = true;
    void fetchRoleDashboard(role).then((payload) => {
      if (!isMounted) return;
      setDashboard(payload);
    });
    return () => {
      isMounted = false;
    };
  }, [role]);

  const generatedFeatureText = generatedRoleExperience[role]?.featureText ?? featureText;
  const resolvedFeatureText = dashboard?.feature_text ?? generatedFeatureText;

  return (
    <section className={styles.page}>
      <ProfileCabinetCard
        displayName={getClientProfileDisplayName(profile)}
        roleLabel={profile.roleLabel}
        photoUrl={profile.photoUrl}
        onClick={() => navigate('/profile')}
      />

      <div className={styles.featureBlock}>
        <div className={styles.featureContent}>
          <span className={styles.featureTitle}>{dashboard?.title ?? generatedRoleExperience[role]?.title}</span>
          <span className={styles.featureText}>{resolvedFeatureText}</span>
          {dashboard?.metrics?.length ? (
            <div className={styles.metricsGrid}>
              {dashboard.metrics.map((metric) => (
                <div key={metric.metric_id} className={styles.metricCard}>
                  <span className={styles.metricLabel}>{metric.label}</span>
                  <strong className={styles.metricValue}>{metric.value}</strong>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
