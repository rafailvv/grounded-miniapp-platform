import { CSSProperties, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { getClientProfileDisplayName, loadRoleProfileView } from '@/shared/profile/clientProfile';
import { persistRoleProfile } from '@/shared/profile/profileApi';
import type { AppRole } from '@/shared/roles/role';
import { useRuntimeManifest } from '@/shared/runtime/RuntimeManifestProvider';
import type { RuntimeAction, RuntimeSection } from '@/shared/runtime/types';
import { ProfileCabinetCard } from '@/shared/ui/ProfileCabinetCard/ProfileCabinetCard';
import styles from '@/shared/ui/generated/GeneratedRoleScreen.module.css';

type GeneratedRoleScreenProps = {
  role: AppRole;
  screenId: string;
};

export function GeneratedRoleScreen({ role, screenId }: GeneratedRoleScreenProps): JSX.Element {
  const location = useLocation();
  const navigate = useNavigate();
  const { manifest, runAction, refresh } = useRuntimeManifest();
  const [formState, setFormState] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string>('');
  const profileView = loadRoleProfileView(role);

  const screen = manifest?.screens[screenId];
  const uiVariant = manifest?.app.ui_variant ?? 'studio';
  const layoutVariant = manifest?.app.layout_variant ?? 'stacked';

  const editableFields = useMemo(() => {
    if (!screen) return [];
    const fields: Array<{ name: string; label: string; value?: string; field_type?: string }> = [];
    screen.sections.forEach((section) => {
      if (section.type === 'form') {
        section.fields.forEach((field) => {
          fields.push({
            name: field.name,
            label: field.label,
            value: field.value ?? field.placeholder ?? '',
            field_type: field.field_type,
          });
        });
      }
      if (section.type === 'profile') {
        section.fields.forEach((field) => {
          fields.push({ name: field.name, label: field.label, value: field.value });
        });
      }
    });
    return fields;
  }, [screen]);

  useEffect(() => {
    const nextState: Record<string, string> = {};
    editableFields.forEach((field) => {
      nextState[field.name] = field.value ?? '';
    });
    setFormState(nextState);
    setMessage('');
  }, [editableFields, screenId]);

  if (!manifest) {
    return <div className={styles.page}>Runtime screen is not available.</div>;
  }

  const variantClass =
    uiVariant === 'atlas'
      ? styles.variantAtlas
      : uiVariant === 'pulse'
        ? styles.variantPulse
        : uiVariant === 'editorial'
          ? styles.variantEditorial
          : styles.variantStudio;
  const layoutClass =
    layoutVariant === 'dashboard'
      ? styles.layoutDashboard
      : layoutVariant === 'stream'
        ? styles.layoutStream
        : layoutVariant === 'minimal'
          ? styles.layoutMinimal
          : layoutVariant === 'magazine'
            ? styles.layoutMagazine
            : styles.layoutStacked;

  const runtimeStyle: CSSProperties = {
    ['--runtime-accent' as string]: manifest.app.theme?.accent ?? undefined,
    ['--runtime-accent-soft' as string]: manifest.app.theme?.accent_soft ?? undefined,
    ['--runtime-surface' as string]: manifest.app.theme?.surface ?? undefined,
    ['--runtime-card' as string]: manifest.app.theme?.card ?? undefined,
    ['--runtime-border' as string]: manifest.app.theme?.border ?? undefined,
  };

  if (!screen) {
    return (
      <section className={`${styles.page} ${styles.variantStudio} ${styles.layoutStacked}`} style={runtimeStyle}>
        <div className={styles.profileEntry}>
          <ProfileCabinetCard
            displayName={getClientProfileDisplayName(profileView)}
            roleLabel={profileView.roleLabel}
            photoUrl={profileView.photoUrl}
            onClick={() => navigate(`/${role}/profile`)}
          />
        </div>
      </section>
    );
  }

  async function handleAction(action: RuntimeAction) {
    if (action.type === 'navigate' && action.target_path) {
      navigate(action.target_path);
      return;
    }

    if (action.type === 'save_profile') {
      await persistRoleProfile(role, {
        first_name: formState.first_name ?? '',
        last_name: formState.last_name ?? '',
        email: formState.email ?? '',
        phone: formState.phone ?? '',
      });
      await refresh();
      setMessage('Profile saved.');
      return;
    }

    const result = await runAction(action.action_id, {
      payload: formState,
      current_path: location.pathname,
      screen_id: screenId,
    });
    if (result.message) {
      setMessage(result.message);
    }
    if (result.next_path) {
      navigate(result.next_path);
    }
  }

  function renderSection(section: RuntimeSection) {
    switch (section.type) {
      case 'heading':
        return (
          <section key={section.section_id} className={styles.sectionBlock}>
            <h1 className={styles.pageTitle}>{section.title}</h1>
            {section.body ? <p className={styles.sectionBody}>{section.body}</p> : null}
          </section>
        );
      case 'hero':
        return (
          <section key={section.section_id} className={styles.sectionBlock}>
            <h2 className={styles.sectionTitle}>{section.title}</h2>
            <p className={styles.sectionBody}>{section.body}</p>
          </section>
        );
      case 'stats':
        return (
          <section key={section.section_id} className={styles.sectionBlock}>
            <div className={styles.statsGrid}>
              {section.items.map((item) => (
                <article key={`${section.section_id}-${item.label}`} className={styles.statCard}>
                  <span className={styles.statLabel}>{item.label}</span>
                  <strong className={styles.statValue}>{item.value}</strong>
                </article>
              ))}
            </div>
          </section>
        );
      case 'list':
        return (
          <section key={section.section_id} className={styles.sectionBlock}>
            {section.items.map((item) => (
              <article key={item.item_id} className={styles.listCard}>
                <strong className={styles.listTitle}>{item.title}</strong>
                <p className={styles.sectionBody}>{item.subtitle}</p>
                <div className={styles.listMeta}>
                  <span className={styles.badge}>{item.status}</span>
                  {item.meta ? <span>{item.meta}</span> : null}
                </div>
              </article>
            ))}
          </section>
        );
      case 'detail':
        return (
          <section key={section.section_id} className={styles.sectionBlock}>
            <h3 className={styles.sectionTitle}>{section.title}</h3>
            <p className={styles.sectionBody}>{section.body}</p>
            <div className={styles.fieldGrid}>
              {section.fields.map((field) => (
                <div key={`${section.section_id}-${field.label}`} className={styles.field}>
                  <span className={styles.fieldLabel}>{field.label}</span>
                  <div>{field.value}</div>
                </div>
              ))}
            </div>
          </section>
        );
      case 'timeline':
        return (
          <section key={section.section_id} className={styles.sectionBlock}>
            <div className={styles.timeline}>
              {section.items.map((item, index) => (
                <div key={`${section.section_id}-${index}`} className={styles.timelineItem}>
                  <strong>{item.label}</strong>
                  <span className={styles.sectionBody}>{item.value}</span>
                </div>
              ))}
            </div>
          </section>
        );
      case 'form':
        return (
          <section key={section.section_id} className={styles.sectionBlock}>
            <div className={styles.fieldGrid}>
              {section.fields.map((field) => (
                <label key={field.field_id} className={styles.field}>
                  <span className={styles.fieldLabel}>{field.label}</span>
                  {field.field_type === 'text' || field.field_type === 'textarea' ? (
                    <textarea
                      className={styles.textarea}
                      value={formState[field.name] ?? ''}
                      onChange={(event) => setFormState((current) => ({ ...current, [field.name]: event.target.value }))}
                    />
                  ) : (
                    <input
                      className={styles.input}
                      value={formState[field.name] ?? ''}
                      onChange={(event) => setFormState((current) => ({ ...current, [field.name]: event.target.value }))}
                    />
                  )}
                </label>
              ))}
            </div>
          </section>
        );
      case 'profile':
        return (
          <section key={section.section_id} className={styles.sectionBlock}>
            <div className={styles.fieldGrid}>
              {section.fields.map((field) => (
                <label key={field.name} className={styles.field}>
                  <span className={styles.fieldLabel}>{field.label}</span>
                  <input
                    className={styles.input}
                    value={formState[field.name] ?? ''}
                    onChange={(event) => setFormState((current) => ({ ...current, [field.name]: event.target.value }))}
                  />
                </label>
              ))}
            </div>
          </section>
        );
      case 'actions':
        return (
          <section key={section.section_id} className={styles.sectionBlock}>
            {section.title ? <h3 className={styles.sectionTitle}>{section.title}</h3> : null}
            <div className={styles.inlineActions}>
              {section.actions.map((action) => (
                <button key={action.action_id} type="button" className={styles.inlineAction} onClick={() => void handleAction(action)}>
                  {action.label}
                </button>
              ))}
            </div>
          </section>
        );
    }
  }

  return (
    <section className={`${styles.page} ${variantClass} ${layoutClass}`} style={runtimeStyle}>
      {location.pathname === '/' ? (
        <div className={styles.profileEntry}>
          <ProfileCabinetCard
            displayName={getClientProfileDisplayName(profileView)}
            roleLabel={profileView.roleLabel}
            photoUrl={profileView.photoUrl}
            onClick={() => navigate(`/${role}/profile`)}
          />
        </div>
      ) : null}

      {screen.sections.map(renderSection)}

      {message ? <div className={styles.message}>{message}</div> : null}
    </section>
  );
}
