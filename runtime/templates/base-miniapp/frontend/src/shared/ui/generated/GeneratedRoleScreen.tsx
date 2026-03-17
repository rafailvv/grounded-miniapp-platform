import { CSSProperties, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { persistRoleProfile } from '@/shared/profile/profileApi';
import type { AppRole } from '@/shared/roles/role';
import { useRuntimeManifest } from '@/shared/runtime/RuntimeManifestProvider';
import type { RuntimeAction, RuntimeSection } from '@/shared/runtime/types';
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

  if (!manifest || !screen) {
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
      case 'hero':
        return (
          <section key={section.section_id} className={styles.sectionCard}>
            <h2 className={styles.heroTitle}>{section.title}</h2>
            <p className={styles.heroBody}>{section.body}</p>
          </section>
        );
      case 'stats':
        return (
          <section key={section.section_id} className={styles.sectionCard}>
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
          <section key={section.section_id} className={styles.sectionCard}>
            {section.items.map((item) => (
              <article key={item.item_id} className={styles.listCard}>
                <strong>{item.title}</strong>
                <p className={styles.heroBody}>{item.subtitle}</p>
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
          <section key={section.section_id} className={styles.sectionCard}>
            <h3 className={styles.heroTitle}>{section.title}</h3>
            <p className={styles.heroBody}>{section.body}</p>
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
          <section key={section.section_id} className={styles.sectionCard}>
            <div className={styles.timeline}>
              {section.items.map((item, index) => (
                <div key={`${section.section_id}-${index}`} className={styles.timelineItem}>
                  <strong>{item.label}</strong>
                  <span className={styles.heroBody}>{item.value}</span>
                </div>
              ))}
            </div>
          </section>
        );
      case 'form':
        return (
          <section key={section.section_id} className={styles.sectionCard}>
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
          <section key={section.section_id} className={styles.sectionCard}>
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
    }
  }

  return (
    <section className={`${styles.page} ${variantClass} ${layoutClass}`} style={runtimeStyle}>
      <header className={styles.screenHeader}>
        <span className={styles.eyebrow}>
          {role} · {manifest.app.generation_mode}
        </span>
        <h1 className={styles.title}>{screen.title}</h1>
        {screen.subtitle ? <p className={styles.subtitle}>{screen.subtitle}</p> : null}
        <div className={styles.runtimeMeta}>
          <span className={styles.metaPill}>{manifest.app.screen_count} screens</span>
          <span className={styles.metaPill}>{manifest.app.route_count} routes</span>
          <span className={styles.metaPill}>{manifest.metrics.length} live metrics</span>
        </div>
      </header>

      <nav
        className={`${styles.navBar} ${uiVariant === 'editorial' ? styles.navBarEditorial : ''} ${
          layoutVariant === 'stream' ? styles.navBarStream : ''
        } ${layoutVariant === 'minimal' ? styles.navBarMinimal : ''}`}
      >
        {manifest.navigation.map((item) => (
          <button
            key={item.path}
            type="button"
            className={`${styles.navChip} ${location.pathname === item.path ? styles.navChipActive : ''}`}
            onClick={() => navigate(item.path)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      {screen.sections.map(renderSection)}

      {message ? <div className={styles.message}>{message}</div> : null}

      <div
        className={`${styles.actionBar} ${uiVariant === 'atlas' || layoutVariant === 'dashboard' ? styles.actionBarStacked : ''} ${
          layoutVariant === 'minimal' ? styles.actionBarMinimal : ''
        }`}
      >
        {screen.actions.map((action, index) => (
          <button
            key={action.action_id}
            type="button"
            className={index === 0 ? styles.primaryButton : styles.secondaryButton}
            onClick={() => void handleAction(action)}
          >
            {action.label}
          </button>
        ))}
      </div>
    </section>
  );
}
